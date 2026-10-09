/* SPDX-License-Identifier: AGPL-3.0-only */
#define _POSIX_C_SOURCE 200809L
#include "surround.h"
#include <libavcodec/avcodec.h>
#include <libavutil/channel_layout.h>
#include <libavutil/opt.h>
#include <libswresample/swresample.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define PENDING 16384
#define PAYLOAD 16384
#define DECODE_SAMPLES 4096
/* Raw DTS core frames span 96..16384 bytes; spacing counts 16-bit words. */
#define RAW_DTS_MIN_SPACING 48
#define RAW_DTS_MAX_SPACING 16384

const char *const surround_positions[8] = {"FL", "FR", "FC", "LFE", "SL", "SR", "BL", "BR"};
enum carrier_mode { MODE_PCM, MODE_ENCODED, MODE_UNKNOWN };
struct pending_frame { uint64_t tag; float pcm[SURROUND_OUTPUTS]; };
struct surround {
    surround_emit emit;
    surround_mute mute;
    void *data;
    struct pending_frame pending[PENDING];
    uint64_t input, emitted, burst_frame, previous_frame, errors, decoded;
    uint64_t words, last_sync, raw_last, raw_spacing;
    uint32_t raw_pattern;
    unsigned raw_repeats, raw_latched;
    enum carrier_mode mode;
    unsigned silence, phase, swapped, kind, bits, payload_words, payload_have, header_error;
    uint16_t previous;
    uint32_t raw_sync;
    uint8_t payload[PAYLOAD + AV_INPUT_BUFFER_PADDING_SIZE];
    AVCodecContext *ac3, *dts, *active;
    AVFrame *frame;
    AVPacket *packet;
    float converted[DECODE_SAMPLES * 8];
    char codec[32], layout[128], mute_reason[32];
    unsigned rate, position_count, position_index[8];
};

static struct pending_frame *slot(surround *d, uint64_t index) {
    struct pending_frame *p = &d->pending[index % PENDING];
    if (p->tag != index) {
        p->tag = index;
        memset(p->pcm, 0, sizeof p->pcm);
    }
    return p;
}

static void metadata(surround *d, const char *codec) {
    snprintf(d->codec, sizeof d->codec, "%s", codec);
    d->rate = SURROUND_RATE;
    d->position_count = 0;
    d->layout[0] = '\0';
    d->mute_reason[0] = '\0';
}

static void pcm_metadata(surround *d) {
    metadata(d, "pcm");
    snprintf(d->layout, sizeof d->layout, "stereo");
    d->position_count = 2;
    d->position_index[0] = 0;
    d->position_index[1] = 1;
}

static void flush_decoders(surround *d) {
    if (d->ac3) {
        /* FFmpeg 8 AC-3 flush also clears fields backing these AVOptions. */
        double drc = 1;
        int64_t target = 0, heavy = 0, noise = 0;
        av_opt_get_double(d->ac3->priv_data, "drc_scale", 0, &drc);
        av_opt_get_int(d->ac3->priv_data, "target_level", 0, &target);
        av_opt_get_int(d->ac3->priv_data, "heavy_compr", 0, &heavy);
        av_opt_get_int(d->ac3->priv_data, "cons_noisegen", 0, &noise);
        avcodec_flush_buffers(d->ac3);
        av_opt_set_double(d->ac3->priv_data, "drc_scale", drc, 0);
        av_opt_set_int(d->ac3->priv_data, "target_level", target, 0);
        av_opt_set_int(d->ac3->priv_data, "heavy_compr", heavy, 0);
        av_opt_set_int(d->ac3->priv_data, "cons_noisegen", noise, 0);
    }
    if (d->dts) avcodec_flush_buffers(d->dts);
    d->active = NULL;
}

static void mute_pending(surround *d, uint64_t start) {
    if (start < d->emitted) start = d->emitted;
    for (uint64_t i = start; i <= d->input + DECODE_SAMPLES; i++)
        memset(slot(d, i)->pcm, 0, sizeof slot(d, i)->pcm);
}

static void unknown(surround *d, const char *reason) {
    if (d->mute) d->mute(d->data, d->input + 1);
    mute_pending(d, d->emitted);
    flush_decoders(d);
    d->mode = MODE_UNKNOWN;
    d->silence = 0;
    d->last_sync = d->input;
    metadata(d, reason);
    snprintf(d->mute_reason, sizeof d->mute_reason, "%s", reason);
    d->errors++;
}

static unsigned duration(unsigned kind) {
    switch (kind) {
    case 1: return 1536;
    case 0x0b: return 512;
    case 0x0c: return 1024;
    case 0x0d: return 2048;
    default: return 4096;
    }
}

static void bad_burst(surround *d, const char *reason) {
    uint64_t start = d->burst_frame < d->emitted ? d->emitted : d->burst_frame;
    uint64_t end = d->burst_frame + duration(d->kind);
    if (end <= d->input) end = d->input + 1;
    for (uint64_t i = start; i < end; i++)
        memset(slot(d, i)->pcm, 0, sizeof slot(d, i)->pcm);
    flush_decoders(d);
    d->mode = MODE_UNKNOWN;
    d->silence = 0;
    if (!d->decoded) metadata(d, reason);
    snprintf(d->mute_reason, sizeof d->mute_reason, "%s", reason);
    d->errors++;
}

static int channel_index(enum AVChannel channel) {
    switch (channel) {
    case AV_CHAN_FRONT_LEFT: return 0;
    case AV_CHAN_FRONT_RIGHT: return 1;
    case AV_CHAN_FRONT_CENTER: return 2;
    case AV_CHAN_LOW_FREQUENCY: return 3;
    case AV_CHAN_SIDE_LEFT: return 4;
    case AV_CHAN_SIDE_RIGHT: return 5;
    case AV_CHAN_BACK_LEFT: return 6;
    case AV_CHAN_BACK_RIGHT: return 7;
    default: return -1;
    }
}

static AVCodecContext *open_decoder(enum AVCodecID id) {
    const AVCodec *codec = avcodec_find_decoder(id);
    AVCodecContext *context = codec ? avcodec_alloc_context3(codec) : NULL;
    if (!context) return NULL;
    context->err_recognition = AV_EF_CRCCHECK | AV_EF_BITSTREAM | AV_EF_BUFFER | AV_EF_EXPLODE;
    context->thread_count = 1;
    if (avcodec_open2(context, codec, NULL) < 0) avcodec_free_context(&context);
    return context;
}

static int receive_packet(surround *d, AVCodecContext *context) {
    av_frame_unref(d->frame);
    if (avcodec_send_packet(context, d->packet) < 0) return -1;
    return avcodec_receive_frame(context, d->frame);
}

static void stereo_matrix(const unsigned *positions, unsigned count, float matrix[2][8]) {
    const float fold = 0.7071067811865476f;
    const float weights[2][8] = {{1, 0, fold, 0.5f, fold, 0, fold, 0},
                                  {0, 1, fold, 0.5f, 0, fold, 0, fold}};
    float sums[2] = {0, 0};
    memset(matrix, 0, sizeof(float) * 16);
    for (unsigned side = 0; side < 2; side++) for (unsigned c = 0; c < count; c++) {
        unsigned position = positions[c];
        matrix[side][position] = weights[side][position];
        sums[side] += weights[side][position];
    }
    /* One layout-dependent gain preserves the stereo image and bounds coherent peaks. */
    float scale = 1.0f / fmaxf(1.0f, fmaxf(sums[0], sums[1]));
    for (unsigned side = 0; side < 2; side++) for (unsigned c = 0; c < 8; c++)
        matrix[side][c] *= scale;
}

static int decode(surround *d) {
    AVCodecContext **owner = d->kind == 1 ? &d->ac3 : &d->dts;
    if (!*owner) *owner = open_decoder(d->kind == 1 ? AV_CODEC_ID_AC3 : AV_CODEC_ID_DTS);
    AVCodecContext *context = *owner;
    if (!context) return -1;
    int continuing = d->active == context && d->position_count;
    if (d->active != context) {
        flush_decoders(d);
        d->active = context;
    }
    av_packet_unref(d->packet);
    if (av_new_packet(d->packet, (int)(d->bits / 8)) < 0) return -1;
    memcpy(d->packet->data, d->payload, d->bits / 8);
    if (receive_packet(d, context) < 0) return -1;
    char received_layout[128];
    if (av_channel_layout_describe(&d->frame->ch_layout, received_layout, sizeof received_layout) < 0) return -1;
    if (continuing && strcmp(received_layout, d->layout)) {
        /* A new layout must not inherit transform history from other positions. */
        flush_decoders(d);
        d->active = context;
        if (receive_packet(d, context) < 0) return -1;
    }
    AVFrame *f = d->frame;
    int channels = f->ch_layout.nb_channels;
    if (f->sample_rate != SURROUND_RATE || channels < 1 || channels > 8 ||
        f->nb_samples < 1 || (unsigned)f->nb_samples != duration(d->kind) ||
        (f->flags & AV_FRAME_FLAG_CORRUPT) || !av_channel_layout_check(&f->ch_layout)) return -1;
    unsigned mapping[8];
    for (int c = 0; c < channels; c++) {
        int position = channel_index(av_channel_layout_channel_from_index(&f->ch_layout, c));
        if (position < 0) return -1;
        mapping[c] = (unsigned)position;
    }
    /* Format conversion only: same rate/layout, no rematrix, SRC or LFE gain. */
    SwrContext *swr = NULL;
    if (swr_alloc_set_opts2(&swr, &f->ch_layout, AV_SAMPLE_FMT_FLT, f->sample_rate,
                           &f->ch_layout, f->format, f->sample_rate, 0, NULL) < 0) return -1;
    uint8_t *output = (uint8_t *)d->converted;
    int count = swr_init(swr) < 0 ? -1 : swr_convert(swr, &output, DECODE_SAMPLES,
                               (const uint8_t **)f->extended_data, f->nb_samples);
    swr_free(&swr);
    if (count != f->nb_samples) return -1;
    for (int i = 0; i < count * channels; i++)
        if (!isfinite(d->converted[i])) return -1;
    metadata(d, d->kind == 1 ? "ac3" : "dts");
    av_channel_layout_describe(&f->ch_layout, d->layout, sizeof d->layout);
    d->position_count = (unsigned)channels;
    memcpy(d->position_index, mapping, (size_t)channels * sizeof mapping[0]);
    float matrix[2][8];
    stereo_matrix(mapping, (unsigned)channels, matrix);
    for (int i = 0; i < count; i++) {
        uint64_t index = d->burst_frame + (unsigned)i;
        if (index < d->emitted) return -1;
        struct pending_frame *p = slot(d, index);
        memset(p->pcm, 0, sizeof p->pcm);
        for (int c = 0; c < channels; c++) p->pcm[mapping[c]] = d->converted[i * channels + c];
        for (unsigned side = 0; side < 2; side++) for (unsigned c = 0; c < 8; c++)
            p->pcm[8 + side] += matrix[side][c] * p->pcm[c];
    }
    d->decoded++;
    return 0;
}

static int raw_dts_sync(uint32_t sync) {
    return sync == 0x7ffe8001 || sync == 0xfe7f0180 || sync == 0x1fffe800 || sync == 0xff1f00e8;
}

/* A lone raw-DTS pattern is ordinary PCM (e.g. -225/+232): raw DTS counts
 * only when its sync recurs three times at one constant frame spacing. */
static void raw_dts_candidate(surround *d) {
    uint64_t spacing = d->words - d->raw_last;
    /* Once confirmed, plausible syncs keep suppression during requalification. */
    if (d->raw_latched && spacing >= RAW_DTS_MIN_SPACING && spacing <= RAW_DTS_MAX_SPACING)
        d->last_sync = d->input;
    if (d->raw_last && d->raw_pattern == d->raw_sync &&
        spacing >= RAW_DTS_MIN_SPACING && spacing <= RAW_DTS_MAX_SPACING) {
        if (spacing == d->raw_spacing) d->raw_repeats++;
        else { d->raw_spacing = spacing; d->raw_repeats = 1; }
    } else {
        d->raw_pattern = d->raw_sync;
        d->raw_spacing = 0;
        d->raw_repeats = 0;
    }
    d->raw_last = d->words;
    if (d->raw_repeats < 2) return;
    d->raw_latched = 1;
    /* Raw DTS decoding is deliberately unsupported, including 14-bit packing. */
    if (d->mode != MODE_UNKNOWN) unknown(d, "raw-dts");
    d->last_sync = d->input;
}

static void reset_raw_dts(surround *d) {
    d->raw_last = d->raw_spacing = 0;
    d->raw_pattern = 0;
    d->raw_repeats = 0;
    d->raw_latched = 0;
}

static void return_to_pcm(surround *d) {
    flush_decoders(d);
    d->mode = MODE_PCM;
    d->previous = d->raw_sync = 0;
    reset_raw_dts(d);
    pcm_metadata(d);
}

static uint16_t swap_word(uint16_t word) { return (uint16_t)((word << 8) | (word >> 8)); }

static void discard_payload(surround *d) {
    unsigned words = (d->bits + 15) / 16;
    unsigned limit = (duration(d->kind) * 4 - 8) / 2;
    if (words > limit) words = limit;
    if (words) {
        d->payload_words = words;
        d->payload_have = 0;
        d->phase = 4;
    }
}

static void word(surround *d, uint16_t value) {
    d->words++;
    if (d->phase == 1) {
        if (d->swapped) value = swap_word(value);
        d->kind = value & 0x1f;
        d->header_error = value & 0x80;
        d->phase = 2;
        return;
    }
    if (d->phase == 2) {
        if (d->swapped) value = swap_word(value);
        d->bits = value;
        d->phase = 0;
        if (d->header_error) { bad_burst(d, "carrier-error"); discard_payload(d); return; }
        if (d->kind == 0 || d->kind == 3) {
            flush_decoders(d);
            metadata(d, d->kind == 0 ? "null" : "pause");
            discard_payload(d);
            return;
        }
        if (d->kind != 1 && d->kind != 0x0b && d->kind != 0x0c && d->kind != 0x0d) {
            bad_burst(d, "unsupported");
            discard_payload(d);
            return;
        }
        unsigned max_bytes = duration(d->kind) * 4 - 8;
        if (!d->bits || d->bits % 8 || d->bits / 8 > max_bytes || d->bits / 8 > PAYLOAD) {
            bad_burst(d, "corrupt");
            discard_payload(d);
            return;
        }
        d->payload_words = (d->bits + 15) / 16;
        d->payload_have = 0;
        d->phase = 3;
        return;
    }
    if (d->phase == 4) {
        if (++d->payload_have == d->payload_words) {
            d->phase = 0;
            d->previous = 0;
            d->raw_sync = 0;
        }
        return;
    }
    if (d->phase == 3) {
        if (d->swapped) value = swap_word(value);
        d->payload[d->payload_have * 2] = (uint8_t)(value >> 8);
        d->payload[d->payload_have * 2 + 1] = (uint8_t)value;
        if (++d->payload_have == d->payload_words) {
            d->phase = 0;
            d->previous = 0;
            d->raw_sync = 0;
            if (decode(d) < 0) bad_burst(d, "corrupt");
        }
        return;
    }
    if ((d->previous == 0xf872 && value == 0x4e1f) ||
        (d->previous == 0x72f8 && value == 0x1f4e)) {
        /* The prefix before a newly recognised preamble may be an unframed
         * payload tail after a switch gap. Suppress it from the earliest
         * still-pending frame; already emitted audio cannot be retracted. */
        d->burst_frame = d->previous_frame;
        if (d->mode == MODE_PCM) mute_pending(d, d->emitted);
        reset_raw_dts(d);
        d->mode = MODE_ENCODED;
        d->silence = 0;
        d->last_sync = d->input;
        d->swapped = d->previous == 0x72f8;
        /* The preceding word may be the right port of the preceding frame. */
        d->phase = 1;
        d->previous = 0;
        d->raw_sync = 0;
        return;
    }
    d->raw_sync = (d->raw_sync << 16) | value;
    if (raw_dts_sync(d->raw_sync)) raw_dts_candidate(d);
    d->previous = value;
    d->previous_frame = d->input;
}

surround *surround_create(surround_emit emit, void *data) {
    surround *d = calloc(1, sizeof *d);
    if (!d) return NULL;
    d->emit = emit;
    d->data = data;
    for (unsigned i = 0; i < PENDING; i++) d->pending[i].tag = UINT64_MAX;
    d->frame = av_frame_alloc();
    d->packet = av_packet_alloc();
    if (!d->frame || !d->packet) { surround_destroy(d); return NULL; }
    pcm_metadata(d);
    return d;
}

void surround_set_mute(surround *d, surround_mute mute) { d->mute = mute; }

void surround_destroy(surround *d) {
    if (!d) return;
    avcodec_free_context(&d->ac3);
    avcodec_free_context(&d->dts);
    av_frame_free(&d->frame);
    av_packet_free(&d->packet);
    free(d);
}

void surround_reset(surround *d) {
    if (d->mute) d->mute(d->data, d->input);
    flush_decoders(d);
    for (unsigned i = 0; i < PENDING; i++) d->pending[i].tag = UINT64_MAX;
    d->input = d->emitted = 0;
    d->previous = d->phase = d->silence = d->raw_sync = 0;
    d->last_sync = 0;
    reset_raw_dts(d);
    d->mode = MODE_UNKNOWN;
    metadata(d, "discontinuity");
    d->errors++;
}

static uint16_t carrier_word(float value) {
    long integer = lroundf(value * 32768.0f);
    if (integer > 32767) integer = 32767;
    if (integer < -32768) integer = -32768;
    return (uint16_t)(int16_t)integer;
}

void surround_push(surround *d, const float *stereo, size_t frames) {
    for (size_t i = 0; i < frames; i++) {
        float l = stereo[i * 2], r = stereo[i * 2 + 1];
        struct pending_frame *p = slot(d, d->input);
        if (!isfinite(l) || !isfinite(r) || fabsf(l) > 1 || fabsf(r) > 1) {
            unknown(d, "corrupt");
            l = r = 0;
        }
        if (d->mode == MODE_PCM) { p->pcm[0] = p->pcm[8] = l; p->pcm[1] = p->pcm[9] = r; }
        if (l == 0 && r == 0 && !d->phase) {
            if (++d->silence >= SURROUND_SILENCE_RESET && d->mode != MODE_PCM) return_to_pcm(d);
            if (d->silence > SURROUND_SILENCE_RESET) d->silence = SURROUND_SILENCE_RESET;
        } else d->silence = 0;
        /* Every IEC stream repeats its preamble each burst; without one the
         * carrier is PCM again, so no latch outlives the stream it guarded. */
        uint64_t timeout = d->raw_latched ? SURROUND_RAW_SYNC_TIMEOUT : SURROUND_SYNC_TIMEOUT;
        if (d->mode != MODE_PCM && !d->phase && d->input - d->last_sync > timeout)
            return_to_pcm(d);
        word(d, carrier_word(l));
        word(d, carrier_word(r));
        d->input++;
        if (d->input > SURROUND_LOOKAHEAD) {
            d->emit(d->data, d->emitted, slot(d, d->emitted)->pcm);
            d->emitted++;
        }
    }
}

void surround_finish(surround *d) {
    if (d->phase) bad_burst(d, "corrupt");
    while (d->emitted < d->input) {
        d->emit(d->data, d->emitted, slot(d, d->emitted)->pcm);
        d->emitted++;
    }
}

int surround_status_json(const surround *d, char *buffer, size_t size) {
    char positions[96] = "";
    size_t offset = 0;
    for (unsigned i = 0; i < d->position_count; i++)
        offset += (size_t)snprintf(positions + offset, sizeof positions - offset,
                                  "%s\"%s\"", i ? "," : "", surround_positions[d->position_index[i]]);
    int n = snprintf(buffer, size,
        "{\"codec\":\"%s\",\"sample_rate\":%u,\"layout\":\"%s\",\"positions\":[%s],"
        "\"bus_positions\":[\"FL\",\"FR\",\"FC\",\"LFE\",\"SL\",\"SR\",\"BL\",\"BR\"],"
        "\"errors\":%llu,\"decoded_bursts\":%llu,\"lookahead_frames\":%u,"
        "\"live_latency_frames\":%u,\"worker_margin_frames\":%u,"
        "\"state\":\"%s\",\"mute_reason\":\"%s\"}\n",
        d->codec, d->rate, d->layout, positions, (unsigned long long)d->errors,
        (unsigned long long)d->decoded, SURROUND_LOOKAHEAD, SURROUND_LIVE_LATENCY,
        SURROUND_LIVE_LATENCY - SURROUND_LOOKAHEAD,
        d->mode == MODE_PCM ? "pcm" : d->mode == MODE_UNKNOWN || !d->position_count ? "muted" : "encoded",
        d->mute_reason);
    return n >= 0 && (size_t)n < size ? n : -1;
}

int surround_write_status(const surround *d, const char *path) {
    if (!path) return 0;
    char buffer[1024];
    int n = surround_status_json(d, buffer, sizeof buffer);
    return n < 0 ? -1 : surround_write_status_json(buffer, path);
}

int surround_write_status_json(const char *json, const char *path) {
    if (!path) return 0;
    char temporary[4096];
    size_t n = strlen(json);
    if (snprintf(temporary, sizeof temporary, "%s.tmp.%ld", path, (long)getpid()) >= (int)sizeof temporary) return -1;
    FILE *file = fopen(temporary, "w");
    if (!file) return -1;
    int error = fwrite(json, 1, n, file) != n;
    if (fclose(file)) error = 1;
    if (!error && rename(temporary, path)) error = 1;
    if (error) unlink(temporary);
    return error ? -1 : 0;
}
