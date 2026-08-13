#define _POSIX_C_SOURCE 200809L
#include "dsp.h"

#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_ROUTES 1024
#define MAX_BIQUADS 32
#define MAX_LINE 4096
#define CONV_BLOCK 256
#define PI 3.14159265358979323846

typedef struct { unsigned in, out; float gain; } route;
typedef struct { float b0, b1, b2, a1, a2, z1, z2; } biquad;
typedef struct { float re, im; } complex_value;
typedef struct {
    size_t tap_count, head_count, head_pos, partition_count, input_pos, spectrum_pos;
    float *head, *head_history, *input_block, *tail_output, *overlap;
    complex_value *ir_spectra, *input_spectra, *work;
} convolution;
typedef struct {
    float gain, polarity;
    size_t delay, delay_pos, delay_size;
    float *delay_line;
    biquad filters[MAX_BIQUADS];
    unsigned filter_count;
    convolution conv;
    float limiter_gain;
    float autogain_square, autogain_gain;
    biquad loudness_low, loudness_high;
    float bass_low, bass_low2, crystal_smooth;
    float peak, square_sum;
    uint64_t meter_frames;
} output_state;

struct fxdsp {
    unsigned rate, inputs, outputs;
    route routes[MAX_ROUTES];
    unsigned route_count;
    output_state out[FXDSP_MAX_CHANNELS];
    float headroom, limiter_threshold, limiter_release;
    float autogain_target, autogain_silence, autogain_history, autogain_smooth;
    float bass_gain, bass_harmonics, bass_alpha, bass_mix;
    float maximizer_ceiling;
    int limiter, autogain, loudness, bass_enhancer, crystalizer, maximizer, bypass;
    unsigned *fft_reverse;
    complex_value *fft_roots;
    _Atomic uint32_t mute_mask;
    _Atomic uint32_t peak_bits[FXDSP_MAX_CHANNELS];
};

static uint32_t float_bits(float value) { uint32_t bits; memcpy(&bits,&value,sizeof bits); return bits; }
static float bits_float(uint32_t bits) { float value; memcpy(&value,&bits,sizeof value); return value; }

static void update_peak(_Atomic uint32_t *peak, float value) {
    uint32_t wanted=float_bits(value),current=atomic_load_explicit(peak,memory_order_relaxed);
    while(bits_float(current)<value&&!atomic_compare_exchange_weak_explicit(peak,&current,wanted,memory_order_relaxed,memory_order_relaxed)) {}
}

static void fail(char *error, size_t size, const char *message) {
    if (error && size) snprintf(error, size, "%s", message);
}

static uint16_t u16le(const unsigned char *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t u32le(const unsigned char *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }

static int load_wav(const char *path, unsigned wanted_channel, float **samples, size_t *count, unsigned wanted_rate) {
    FILE *file = fopen(path, "rb");
    unsigned char header[12], chunk[8], fmt[40];
    uint16_t format = 0, channels = 0, bits = 0;
    uint32_t rate = 0, data_size = 0;
    long data_offset = 0;
    if (!file || fread(header, 1, 12, file) != 12 || memcmp(header, "RIFF", 4) || memcmp(header + 8, "WAVE", 4)) goto bad;
    while (fread(chunk, 1, 8, file) == 8) {
        uint32_t size = u32le(chunk + 4);
        if (!memcmp(chunk, "fmt ", 4)) {
            if (size < 16 || size > sizeof fmt || fread(fmt, 1, size, file) != size) goto bad;
            format = u16le(fmt); channels = u16le(fmt + 2); rate = u32le(fmt + 4); bits = u16le(fmt + 14);
        } else if (!memcmp(chunk, "data", 4)) {
            data_offset = ftell(file); data_size = size;
            if (fseek(file, size + (size & 1), SEEK_CUR)) goto bad;
        } else if (fseek(file, size + (size & 1), SEEK_CUR)) goto bad;
    }
    if (!data_offset || !channels || wanted_channel >= channels || rate != wanted_rate || !((format == 1 && (bits == 16 || bits == 24 || bits == 32)) || (format == 3 && bits == 32))) goto bad;
    size_t bytes = bits / 8, frames = data_size / (bytes * channels);
    float *result = calloc(frames, sizeof *result);
    unsigned char raw[4];
    if (!result || fseek(file, data_offset, SEEK_SET)) { free(result); goto bad; }
    for (size_t i = 0; i < frames; i++) {
        for (unsigned c = 0; c < channels; c++) {
            if (fread(raw, 1, bytes, file) != bytes) { free(result); goto bad; }
            if (c != wanted_channel) continue;
            if (format == 3) memcpy(&result[i], raw, 4);
            else if (bits == 16) result[i] = (int16_t)u16le(raw) / 32768.0f;
            else if (bits == 24) { int32_t value = raw[0] | raw[1] << 8 | raw[2] << 16; if (value & 0x800000) value |= ~0xffffff; result[i] = value / 8388608.0f; }
            else result[i] = (int32_t)u32le(raw) / 2147483648.0f;
        }
    }
    fclose(file); *samples = result; *count = frames; return 0;
bad:
    if (file) fclose(file);
    return -1;
}

static void fft(const fxdsp *d, complex_value *values, int inverse) {
    const size_t size = CONV_BLOCK * 2;
    for (size_t i = 0; i < size; i++) if (d->fft_reverse[i] > i) {
        complex_value swap = values[i]; values[i] = values[d->fft_reverse[i]]; values[d->fft_reverse[i]] = swap;
    }
    for (size_t width = 2; width <= size; width *= 2) {
        size_t step = size / width;
        for (size_t base = 0; base < size; base += width) for (size_t j = 0; j < width / 2; j++) {
            complex_value root = d->fft_roots[j * step];
            if (inverse) root.im = -root.im;
            complex_value right = values[base + j + width / 2];
            complex_value product = {right.re * root.re - right.im * root.im, right.re * root.im + right.im * root.re};
            complex_value left = values[base + j];
            values[base + j].re = left.re + product.re; values[base + j].im = left.im + product.im;
            values[base + j + width / 2].re = left.re - product.re; values[base + j + width / 2].im = left.im - product.im;
        }
    }
    if (inverse) for (size_t i = 0; i < size; i++) { values[i].re /= size; values[i].im /= size; }
}

static int prepare_convolution(fxdsp *d, convolution *c, float *taps, size_t count) {
    const size_t fft_size = CONV_BLOCK * 2;
    c->tap_count = count; c->head_count = count < CONV_BLOCK ? count : CONV_BLOCK;
    c->partition_count = count > CONV_BLOCK ? (count - CONV_BLOCK + CONV_BLOCK - 1) / CONV_BLOCK : 0;
    c->head = calloc(c->head_count, sizeof *c->head); c->head_history = calloc(c->head_count, sizeof *c->head_history);
    if (c->partition_count) {
        c->input_block = calloc(CONV_BLOCK, sizeof *c->input_block); c->tail_output = calloc(CONV_BLOCK, sizeof *c->tail_output);
        c->overlap = calloc(CONV_BLOCK, sizeof *c->overlap); c->work = calloc(fft_size, sizeof *c->work);
        c->ir_spectra = calloc(c->partition_count * fft_size, sizeof *c->ir_spectra);
        c->input_spectra = calloc(c->partition_count * fft_size, sizeof *c->input_spectra);
    }
    if (!c->head || !c->head_history || (c->partition_count && (!c->input_block || !c->tail_output || !c->overlap || !c->work || !c->ir_spectra || !c->input_spectra))) return -1;
    memcpy(c->head, taps, c->head_count * sizeof *taps);
    for (size_t p = 0; p < c->partition_count; p++) {
        complex_value *spectrum = c->ir_spectra + p * fft_size;
        size_t offset = CONV_BLOCK + p * CONV_BLOCK;
        size_t length = count - offset < CONV_BLOCK ? count - offset : CONV_BLOCK;
        for (size_t i = 0; i < length; i++) spectrum[i].re = taps[offset + i];
        fft(d, spectrum, 0);
    }
    return 0;
}

static float convolve(fxdsp *d, convolution *c, float input) {
    double direct = 0;
    c->head_history[c->head_pos] = input;
    size_t history = c->head_pos;
    for (size_t i = 0; i < c->head_count; i++) { direct += c->head[i] * c->head_history[history]; history = history ? history - 1 : c->head_count - 1; }
    c->head_pos = (c->head_pos + 1) % c->head_count;
    if (!c->partition_count) return (float)direct;
    float tail = c->tail_output[c->input_pos];
    c->input_block[c->input_pos++] = input;
    if (c->input_pos == CONV_BLOCK) {
        const size_t fft_size = CONV_BLOCK * 2;
        c->spectrum_pos = (c->spectrum_pos + 1) % c->partition_count;
        complex_value *current = c->input_spectra + c->spectrum_pos * fft_size;
        memset(current, 0, fft_size * sizeof *current);
        for (size_t i = 0; i < CONV_BLOCK; i++) current[i].re = c->input_block[i];
        fft(d, current, 0); memset(c->work, 0, fft_size * sizeof *c->work);
        for (size_t p = 0; p < c->partition_count; p++) {
            size_t x_index = (c->spectrum_pos + c->partition_count - p) % c->partition_count;
            complex_value *x = c->input_spectra + x_index * fft_size, *h = c->ir_spectra + p * fft_size;
            for (size_t i = 0; i < fft_size; i++) { c->work[i].re += x[i].re*h[i].re-x[i].im*h[i].im; c->work[i].im += x[i].re*h[i].im+x[i].im*h[i].re; }
        }
        fft(d, c->work, 1);
        for (size_t i = 0; i < CONV_BLOCK; i++) { c->tail_output[i] = c->work[i].re + c->overlap[i]; c->overlap[i] = c->work[i + CONV_BLOCK].re; }
        c->input_pos = 0;
    }
    return (float)direct + tail;
}

static int design(biquad *b, const char *type, float rate, float frequency, float q, float gain_db) {
    if (!(frequency > 0 && frequency < rate * .5f && q > 0)) return -1;
    double a = pow(10.0, gain_db / 40.0), w = 2.0 * PI * frequency / rate;
    double cs = cos(w), sn = sin(w), alpha = sn / (2.0 * q), beta = 2.0 * sqrt(a) * alpha;
    double b0, b1, b2, a0, a1, a2;
    if (!strcmp(type, "bell")) { b0=1+alpha*a; b1=-2*cs; b2=1-alpha*a; a0=1+alpha/a; a1=-2*cs; a2=1-alpha/a; }
    else if (!strcmp(type, "notch")) { b0=1; b1=-2*cs; b2=1; a0=1+alpha; a1=-2*cs; a2=1-alpha; }
    else if (!strcmp(type, "lowpass")) { b0=(1-cs)/2; b1=1-cs; b2=(1-cs)/2; a0=1+alpha; a1=-2*cs; a2=1-alpha; }
    else if (!strcmp(type, "highpass")) { b0=(1+cs)/2; b1=-(1+cs); b2=(1+cs)/2; a0=1+alpha; a1=-2*cs; a2=1-alpha; }
    else if (!strcmp(type, "lowshelf")) { b0=a*((a+1)-(a-1)*cs+beta); b1=2*a*((a-1)-(a+1)*cs); b2=a*((a+1)-(a-1)*cs-beta); a0=(a+1)+(a-1)*cs+beta; a1=-2*((a-1)+(a+1)*cs); a2=(a+1)+(a-1)*cs-beta; }
    else if (!strcmp(type, "highshelf")) { b0=a*((a+1)+(a-1)*cs+beta); b1=-2*a*((a-1)+(a+1)*cs); b2=a*((a+1)+(a-1)*cs-beta); a0=(a+1)-(a-1)*cs+beta; a1=2*((a-1)-(a+1)*cs); a2=(a+1)-(a-1)*cs-beta; }
    else return -1;
    b->b0=b0/a0; b->b1=b1/a0; b->b2=b2/a0; b->a1=a1/a0; b->a2=a2/a0; b->z1=b->z2=0; return 0;
}

static float run_biquad(biquad *b, float value) {
    float output = value * b->b0 + b->z1;
    b->z1 = value * b->b1 - b->a1 * output + b->z2;
    b->z2 = value * b->b2 - b->a2 * output;
    return output;
}

fxdsp *fxdsp_load(const char *path, char *error, size_t error_size) {
    FILE *file = fopen(path, "r");
    fxdsp *d = calloc(1, sizeof *d);
    char line[MAX_LINE], arg[1024], type[32], extra;
    unsigned line_no = 0, out, in, channel;
    if (!file || !d) { fail(error,error_size,"cannot open config"); if(file)fclose(file); free(d); return NULL; }
    d->rate=48000; d->headroom=1; d->limiter_threshold=1; d->maximizer_ceiling=1;
    for (unsigned i=0;i<FXDSP_MAX_CHANNELS;i++) { d->out[i].gain=1; d->out[i].polarity=1; d->out[i].limiter_gain=1; d->out[i].autogain_gain=1; }
    while (fgets(line,sizeof line,file)) {
        line_no++; char *p=line; while(*p==' '||*p=='\t')p++; if(*p=='#'||*p=='\n'||!*p)continue;
        if (sscanf(p,"rate %u %c",&d->rate,&extra)==1) {}
        else if (sscanf(p,"inputs %u %c",&d->inputs,&extra)==1) {}
        else if (sscanf(p,"outputs %u %c",&d->outputs,&extra)==1) {}
        else { float route_gain;
        if (sscanf(p,"matrix %u %u %f %c",&out,&in,&route_gain,&extra)==3 && d->route_count<MAX_ROUTES) { d->routes[d->route_count].out=out; d->routes[d->route_count].in=in; d->routes[d->route_count++].gain=route_gain; }
        else { float x,y,z; int enabled;
            if (sscanf(p,"output %u %f %f %31s %c",&out,&x,&y,arg,&extra)==4 && out<FXDSP_MAX_CHANNELS) { d->out[out].gain=powf(10,x/20); d->out[out].delay=(size_t)llround(y*d->rate/1000); d->out[out].polarity=!strcmp(arg,"invert")?-1:1; }
            else if (sscanf(p,"peq %u %31s %f %f %f %c",&out,type,&x,&y,&z,&extra)==5 && out<FXDSP_MAX_CHANNELS && d->out[out].filter_count<MAX_BIQUADS && !design(&d->out[out].filters[d->out[out].filter_count],type,d->rate,x,y,z)) d->out[out].filter_count++;
            else if (sscanf(p,"ir %u %1023s %u %c",&out,arg,&channel,&extra)==3 && out<FXDSP_MAX_CHANNELS && !d->out[out].conv.head && !load_wav(arg,channel,&d->out[out].conv.head,&d->out[out].conv.tap_count,d->rate)) {}
            else if (sscanf(p,"ir %u %1023s %c",&out,arg,&extra)==2 && out<FXDSP_MAX_CHANNELS && !d->out[out].conv.head && !load_wav(arg,0,&d->out[out].conv.head,&d->out[out].conv.tap_count,d->rate)) {}
            else if (sscanf(p,"headroom_db %f %c",&x,&extra)==1) d->headroom=powf(10,x/20);
            else if (sscanf(p,"limiter %f %f %c",&x,&y,&extra)==2 && y>0) { d->limiter=1; d->limiter_threshold=powf(10,x/20); d->limiter_release=expf(-1/(y*.001f*d->rate)); }
            else if (sscanf(p,"autogain %f %f %f %c",&x,&y,&z,&extra)==3 && !d->autogain && isfinite(x) && isfinite(y) && isfinite(z) && x>=-60 && x<=0 && y>=-100 && y<x && z>=.05f && z<=60) { d->autogain=1; d->autogain_target=powf(10,x/20); d->autogain_silence=powf(10,y/20); d->autogain_history=expf(-1/(z*d->rate)); d->autogain_smooth=expf(-1/(.05f*d->rate)); }
            else if (sscanf(p,"loudness %f %f %c",&x,&y,&extra)==2 && !d->loudness && isfinite(x) && isfinite(y) && x>=-80 && x<=0 && y>=1 && y<=10) { float depth=(-x/80)*(y/10); d->loudness=1; for(unsigned i=0;i<FXDSP_MAX_CHANNELS;i++) if(design(&d->out[i].loudness_low,"lowshelf",d->rate,180,.70710678f,12*depth)||design(&d->out[i].loudness_high,"highshelf",d->rate,5000,.70710678f,5*depth)) goto invalid; }
            else if (sscanf(p,"bass_enhancer %f %f %f %f %c",&x,&y,&z,&route_gain,&extra)==4 && !d->bass_enhancer && isfinite(x) && isfinite(y) && isfinite(z) && isfinite(route_gain) && x>=-20 && x<=20 && y>=1 && y<=20 && z>=20 && z<=500 && route_gain>=-100 && route_gain<=100) { d->bass_enhancer=1; d->bass_gain=powf(10,x/20); d->bass_harmonics=y; d->bass_alpha=1-expf(-2*PI*z/d->rate); d->bass_mix=(route_gain+100)/200; }
            else if (sscanf(p,"crystalizer %c",&extra)==EOF && !d->crystalizer) d->crystalizer=1;
            else if (sscanf(p,"maximizer %f %c",&x,&extra)==1 && !d->maximizer && isfinite(x) && x>=-20 && x<=0) { d->maximizer=1; d->maximizer_ceiling=powf(10,x/20); }
            else if (sscanf(p,"bypass %d %c",&enabled,&extra)==1) d->bypass=!!enabled;
            else { invalid: snprintf(line,sizeof line,"invalid config line %u",line_no); fail(error,error_size,line); goto bad; }
        }}
    }
    fclose(file); file=NULL;
    if (!d->rate || d->inputs<1 || d->inputs>32 || d->outputs<1 || d->outputs>32) { fail(error,error_size,"inputs/outputs must be 1..32"); goto bad; }
    for(unsigned r=0;r<d->route_count;r++) if(d->routes[r].in>=d->inputs||d->routes[r].out>=d->outputs){fail(error,error_size,"matrix index out of range");goto bad;}
    d->fft_reverse=calloc(CONV_BLOCK*2,sizeof *d->fft_reverse);d->fft_roots=calloc(CONV_BLOCK,sizeof *d->fft_roots);
    if(!d->fft_reverse||!d->fft_roots){fail(error,error_size,"out of memory");goto bad;}
    for(unsigned i=0;i<CONV_BLOCK*2;i++){unsigned value=i,reversed=0;for(unsigned bit=0;bit<9;bit++){reversed=(reversed<<1)|(value&1);value>>=1;}d->fft_reverse[i]=reversed;}
    for(unsigned i=0;i<CONV_BLOCK;i++){double angle=-2*PI*i/(CONV_BLOCK*2);d->fft_roots[i].re=cos(angle);d->fft_roots[i].im=sin(angle);}
    for(out=0;out<d->outputs;out++) { output_state *s=&d->out[out]; s->delay_size=s->delay+1; s->delay_line=calloc(s->delay_size,sizeof(float)); if(s->conv.tap_count){float*taps=s->conv.head;size_t count=s->conv.tap_count;memset(&s->conv,0,sizeof s->conv);if(prepare_convolution(d,&s->conv,taps,count)){free(taps);fail(error,error_size,"out of memory");goto bad;}free(taps);} if(!s->delay_line){fail(error,error_size,"out of memory");goto bad;} }
    return d;
bad:
    if (file) fclose(file);
    fxdsp_free(d);
    return NULL;
}

void fxdsp_free(fxdsp *d) { if(!d)return; for(unsigned i=0;i<32;i++){convolution*c=&d->out[i].conv;free(d->out[i].delay_line);free(c->head);free(c->head_history);free(c->input_block);free(c->tail_output);free(c->overlap);free(c->ir_spectra);free(c->input_spectra);free(c->work);}free(d->fft_reverse);free(d->fft_roots);free(d); }
unsigned fxdsp_inputs(const fxdsp*d){return d->inputs;} unsigned fxdsp_outputs(const fxdsp*d){return d->outputs;} unsigned fxdsp_rate(const fxdsp*d){return d->rate;}

void fxdsp_process(fxdsp *d, const float *const *input, float *const *output, size_t frames) {
    for(size_t n=0;n<frames;n++) { uint32_t mute_mask=atomic_load_explicit(&d->mute_mask,memory_order_relaxed); for(unsigned o=0;o<d->outputs;o++) {
        output_state *s=&d->out[o]; float value=0;
        if(d->bypass) value=o<d->inputs?input[o][n]:0;
        else {
            for(unsigned r=0;r<d->route_count;r++) if(d->routes[r].out==o)value+=input[d->routes[r].in][n]*d->routes[r].gain;
            for(unsigned f=0;f<s->filter_count;f++) value=run_biquad(&s->filters[f],value);
            if(s->conv.tap_count)value=convolve(d,&s->conv,value);
            s->delay_line[s->delay_pos]=value;size_t rp=(s->delay_pos+s->delay_size-s->delay)%s->delay_size;value=s->delay_line[rp];s->delay_pos=(s->delay_pos+1)%s->delay_size;
            value*=s->gain*s->polarity*d->headroom;
            if(!isfinite(value)) value=0;
            if(d->bass_enhancer){s->bass_low+=d->bass_alpha*(value-s->bass_low);s->bass_low2+=d->bass_alpha*(s->bass_low-s->bass_low2);float harmonic=(tanhf(s->bass_low2*d->bass_harmonics)-s->bass_low2)*.25f;float wet=value+s->bass_low2*(d->bass_gain-1)+harmonic*(d->bass_gain-1);value+=d->bass_mix*(wet-value);}
            if(d->autogain){float magnitude=fabsf(value);if(magnitude>=d->autogain_silence){s->autogain_square=d->autogain_history*s->autogain_square+(1-d->autogain_history)*value*value;float level=sqrtf(s->autogain_square);float wanted=level>1e-12f?d->autogain_target/level:1;wanted=fminf(4,fmaxf(.25f,wanted));s->autogain_gain=d->autogain_smooth*s->autogain_gain+(1-d->autogain_smooth)*wanted;}value*=s->autogain_gain;value=fminf(.4f,fmaxf(-.4f,value));}
            if(d->loudness){value=run_biquad(&s->loudness_low,value);value=run_biquad(&s->loudness_high,value);}
            if(d->crystalizer){s->crystal_smooth+=.08f*(value-s->crystal_smooth);value+=.25f*(value-s->crystal_smooth);}
            if(d->maximizer)value=d->maximizer_ceiling*tanhf(value/d->maximizer_ceiling);
            if(d->limiter){float magnitude=fabsf(value),wanted=magnitude>d->limiter_threshold?d->limiter_threshold/magnitude:1;if(wanted<s->limiter_gain)s->limiter_gain=wanted;else s->limiter_gain=1-(1-s->limiter_gain)*d->limiter_release;value*=s->limiter_gain;}
            if(!isfinite(value)) value=0;
        }
        if(mute_mask&(UINT32_C(1)<<o))value=0;
        output[o][n]=value;float magnitude=fabsf(value);if(magnitude>s->peak)s->peak=magnitude;update_peak(&d->peak_bits[o],magnitude);s->square_sum+=value*value;s->meter_frames++;
    }}
}
void fxdsp_meter(const fxdsp*d,unsigned o,float*p,float*r){if(o>=d->outputs){*p=*r=0;return;}*p=d->out[o].peak;*r=d->out[o].meter_frames?sqrtf(d->out[o].square_sum/d->out[o].meter_frames):0;}
void fxdsp_set_mute(fxdsp*d,uint32_t mask,int muted){if(muted)atomic_fetch_or_explicit(&d->mute_mask,mask,memory_order_relaxed);else atomic_fetch_and_explicit(&d->mute_mask,~mask,memory_order_relaxed);}
unsigned fxdsp_peaks(const fxdsp*d,float*peaks,unsigned count){unsigned total=d->outputs,limit=count<total?count:total;for(unsigned o=0;o<limit;o++)peaks[o]=bits_float(atomic_load_explicit(&d->peak_bits[o],memory_order_relaxed));return total;}
void fxdsp_reset_peaks(fxdsp*d){for(unsigned o=0;o<d->outputs;o++)atomic_store_explicit(&d->peak_bits[o],0,memory_order_relaxed);}
