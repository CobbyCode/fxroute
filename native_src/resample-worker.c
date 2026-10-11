/* SPDX-License-Identifier: AGPL-3.0-only */
#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdatomic.h>
#include <time.h>
#include <string.h>
#include <unistd.h>
#include "resample-worker.h"
#include "policy.h"
#ifdef FX_SRC_VERIFY
#include "verify.h"
#endif

#define BUFFER 16384
#define STARTUP_WAIT_NS 3750000000ULL
#define MAX_RATE 768000U
static uint64_t startup_now_ns(void)
{ struct timespec now; clock_gettime(CLOCK_MONOTONIC,&now); return (uint64_t)now.tv_sec*1000000000+now.tv_nsec; }
struct composition {
    struct fx_worker *worker;
    struct resample match;
    float **buffer, **startup;
    unsigned startup_size, startup_read;
    _Atomic uint32_t startup_count;
    _Atomic uint32_t buffered;
    uint32_t flush;
    uint64_t accepted, nominal_pulled, first_input_ns;
    _Atomic uint64_t real, lead, reserve, first_real_ns;
    double rate, fraction, prime, startup_input_delay, rate_min, rate_max;
    uint64_t rate_updates, max_callback_ns, callbacks;
    unsigned eof, flushed, done, total_underruns;
    _Atomic unsigned underruns;
    enum fx_profile profile;
    _Atomic int error;
};
const char *fx_resample_profile(struct resample *r)
{ return r->data?fx_src_profile_name(((struct composition *)r->data)->profile):"unconfigured"; }
static void free_composition(struct resample *r)
{
    struct composition *c=r->data;if(!c)return;
    struct fx_worker_stats stats={0};if(c->worker)fx_worker_stats(c->worker,&stats);
    /* Destruction is outside the graph process callback. */
    fprintf(stderr,"FXSRC_WORKER_STATS {\"mode\":\"%s\",\"channels\":%u,\"input_rate\":%u,\"output_rate\":%u,\"spawns\":%u,"
        "\"max_callback_ms\":%.6f,\"callbacks\":%lu,\"underruns\":%u,\"max_input\":%lu,\"max_output\":%lu,"
        "\"accepted\":%lu,\"produced\":%lu,\"delivered\":%lu,\"lead\":%lu,\"reserve\":%lu,"
        "\"first_output_ns\":%lu,\"first_real_ns\":%lu,\"rate_min\":%.9f,\"rate_max\":%.9f,\"error\":%d}\n",
        fx_src_profile_name(c->profile),r->channels,r->i_rate,r->o_rate,stats.spawns,
        c->max_callback_ns/1e6,c->callbacks,c->total_underruns,stats.max_input,stats.max_output,
        stats.accepted,stats.produced,stats.delivered,(uint64_t)c->lead,(uint64_t)c->reserve,
        stats.first_output_ns,(uint64_t)c->first_real_ns,c->rate_min,c->rate_max,c->error?c->error:stats.error);
    if(c->worker)fx_worker_destroy(c->worker);
    if(c->match.free)resample_free(&c->match);
    if(c->buffer)for(unsigned i=0;i<r->channels;++i)free(c->buffer[i]);
    if(c->startup)for(unsigned i=0;i<r->channels;++i)free(c->startup[i]);
    free(c->startup);free(c->buffer);free(c);r->data=NULL;
}
static uint32_t input_length(struct resample *r,uint32_t output)
{
    struct composition *c=r->data;
    if(c->eof)return 0;
    if(!c->worker){unsigned need=resample_in_len(&c->match,output);return need>c->buffered?need-c->buffered:0;}
    if(!output)return 0;
    double demand=output*(double)r->i_rate/(r->o_rate*c->rate)+c->fraction;
    return demand>0?floor(demand):0;
}
static uint32_t output_length(struct resample *r,uint32_t input)
{
    struct composition *c=r->data;
    if(!c->worker)return resample_out_len(&c->match,input+c->buffered);
    double ratio=(double)r->i_rate/(r->o_rate*c->rate);
    double available=(input+1.0-c->fraction)/ratio;
    return available>1?ceil(available)-1:0;
}
static void update_rate(struct resample *r,double rate)
{
    struct composition *c=r->data;
    ++c->rate_updates;
    if(rate<c->rate_min)c->rate_min=rate;
    if(rate>c->rate_max)c->rate_max=rate;
    c->rate=rate;resample_update_rate(&c->match,rate);
}
static double timeline_delay(struct resample *r)
{
    struct composition *c=r->data;
    if(c->prime)return .750*r->i_rate;
    double outstanding=c->accepted-(c->nominal_pulled-c->buffered-c->startup_count)*(double)r->i_rate/r->o_rate;
    double delay=outstanding+((double)resample_delay(&c->match)+resample_phase(&c->match))*(double)r->i_rate/r->o_rate;
    return delay>0?delay:0;
}
double fx_resample_match_delay(struct resample *r)
{
    struct composition *c=r->data;
    /* Startup padding is fixed processing latency, not excess source data.
     * Feeding it to a device servo makes Bluetooth drop its receive buffer. */
    if(c->worker&&c->prime)return 0;
    return fmax(0,timeline_delay(r)-c->startup_input_delay);
}
static uint32_t delay_frames(struct resample *r) { return floor(timeline_delay(r)); }
static float delay_phase(struct resample *r) { double d=timeline_delay(r);return d-floor(d); }
static void reset_composition(struct resample *r)
{
    struct composition *c=r->data;
    /* SPA can flush after a failed setup or after releasing the converter. */
    if(!c)return;
    if(c->worker)fx_worker_reset(c->worker);
    resample_reset(&c->match);
    c->buffered=c->flush=0;
    c->eof=c->flushed=c->done=c->underruns=0;c->error=0;
    c->startup_count=0;c->startup_read=0;
    c->accepted=c->nominal_pulled=c->real=c->lead=c->reserve=0;c->fraction=0;
    c->startup_input_delay=0;
    c->first_input_ns=0;c->first_real_ns=0;
    c->prime=c->worker?ceil(.750*r->o_rate):0;
}
void fx_resample_finish(struct resample *r)
{
    struct composition *c=r->data;
    if(c->eof)return;
    c->eof=1;
    if(c->worker)fx_worker_finish(c->worker);
    if(!c->accepted){c->prime=0;c->done=1;}
#ifdef FX_SRC_VERIFY
    fx_verify_account(0,0,c->rate,1,c->prime!=0);
#endif
}
int fx_resample_finished(struct resample *r) { return ((struct composition *)r->data)->done; }
int fx_resample_error(struct resample *r)
{
    struct composition *c=r->data;
    return c->error?c->error:(c->worker?fx_worker_error(c->worker):0);
}
void fx_resample_stats(struct resample *r,struct fx_resample_stats *stats)
{
    struct composition *c=r->data;
    memset(stats,0,sizeof(*stats));
    if(c->worker)fx_worker_stats(c->worker,&stats->worker);
    /* reserve before lead and real before first_real_ns: each counter is
     * published after the value it is checked against. */
    stats->reserve_frames=c->reserve;stats->lead_frames=c->lead;
    stats->real_frames=c->real;stats->first_real_ns=c->first_real_ns;stats->underruns=c->underruns;
    stats->buffered=c->buffered+c->startup_count;
}
void fx_resample_note_callback(struct resample *r,uint64_t ns)
{
    struct composition *c=r->data;
    if(ns>c->max_callback_ns)c->max_callback_ns=ns;
    ++c->callbacks;
}
/* First real output latency, written once on the processing thread before
 * the real frames are published. */
static void add_real(struct composition *c,unsigned made)
{
    if(made&&!c->first_real_ns){uint64_t ns=c->first_input_ns?startup_now_ns()-c->first_input_ns:0;c->first_real_ns=ns?ns:1;}
    c->real+=made;
}
/* Startup timeout is its own terminal error: it never replaces an existing
 * error and is not a queue underrun. */
static int startup_expired(struct composition *c)
{
    if(!c->worker||c->eof||c->error||!c->first_input_ns||c->real||fx_worker_error(c->worker)||
       startup_now_ns()-c->first_input_ns<=STARTUP_WAIT_NS)return 0;
    c->error=ETIMEDOUT;
    return 1;
}
/* Retain nominal PCM while startup silence is emitted. All storage is
 * allocated at setup; two bounded pulls handle wrap without realtime allocation. */
static void collect_startup(struct resample *r)
{
    struct composition *c=r->data;
    for(unsigned pass=0;pass<2;++pass) {
        unsigned count=c->startup_count,room=c->startup_size-count;
        if(!room)break;
        unsigned write=(c->startup_read+count)%c->startup_size;
        unsigned wanted=c->startup_size-write;if(wanted>room)wanted=room;
        float *pointers[FX_WORKER_MAX_CHANNELS];
        for(unsigned i=0;i<r->channels;++i)pointers[i]=c->startup[i]+write;
        unsigned pulled=fx_worker_pull(c->worker,pointers,wanted);
        c->startup_count+=pulled;c->nominal_pulled+=pulled;
        if(pulled<wanted)break;
    }
}
static void process_composition(struct resample *r,const void * SPA_RESTRICT src[],uint32_t *input,
    void * SPA_RESTRICT dst[],uint32_t *output)
{
    struct composition *c=r->data;
    unsigned offered=*input,capacity=*output;*input=*output=0;
    if(startup_expired(c))return;
    if(c->worker&&!c->real)collect_startup(r);
    if(c->done||c->error||(!capacity&&(c->worker||c->eof))) {
        return;
    }
    if(!c->eof) {
        unsigned take=offered;
        if(c->worker) {
            unsigned wanted=input_length(r,capacity);if(take>wanted)take=wanted;
            take=fx_worker_push(c->worker,(const float *const *)src,take);
            unsigned available=output_length(r,take);
            /* Existing nominal PCM must drain even when the input queue is full.
             * Fractional input debt is carried into the next callback, not dropped. */
            if(!c->prime) {
                unsigned queued=c->buffered+c->startup_count+fx_worker_available(c->worker);
                if(queued>BUFFER)queued=BUFFER;
                unsigned ready=resample_out_len(&c->match,queued);
                if(ready>available)available=ready;
            }
            if(capacity>available)capacity=available;
            c->fraction-=take;
        } else {
            unsigned made=capacity;
            resample_process(&c->match,src,&take,dst,&made);
#ifdef FX_SRC_VERIFY
            fx_verify_match(c->rate,src,offered,capacity,take,made,dst);
            fx_verify_account(take,made,c->rate,0,0);
#endif
            *output=made;add_real(c,made);c->nominal_pulled+=take;
        }
        if(take&&!c->first_input_ns)c->first_input_ns=startup_now_ns();
        *input=take;c->accepted+=take;
        if(!c->worker)return;
        if(!capacity){
            if(startup_expired(c))return;
#ifdef FX_SRC_VERIFY
            fx_verify_account(take,0,c->rate,0,c->prime!=0);
#endif
            return;
        }
    }
    if(c->prime) {
        /* Track reserve in nominal frames, not rate-scaled output frames. */
        double remaining=ceil(c->prime*c->rate);
        unsigned made=capacity<remaining?capacity:remaining;
        for(unsigned i=0;i<r->channels;++i)memset(dst[i],0,made*4);
        c->prime=fmax(0,c->prime-made/c->rate);c->lead+=made;c->reserve+=made;*output=made;
        c->startup_input_delay+=made*(double)r->i_rate/(r->o_rate*c->rate);
#ifdef FX_SRC_VERIFY
        fx_verify_account(*input,made,c->rate,c->eof,c->prime!=0);
#endif
        if(!c->eof)c->fraction+=made*(double)r->i_rate/(r->o_rate*c->rate);
        return;
    }
    unsigned need=resample_in_len(&c->match,capacity);
    /* Engine output arrives in bursts. Do not spend the reserve on the first
     * short burst; retain a nominal reserve before releasing real PCM. */
    unsigned ready_frames=(double)r->i_rate/r->o_rate>4.0 ? (3ULL*r->o_rate+3)/4 : need;
    unsigned startup_ready=!c->worker||c->real||c->eof||
        c->buffered+c->startup_count+fx_worker_available(c->worker)>=ready_frames;
    if(c->worker&&c->buffered<need) {
        unsigned wanted=need-c->buffered;if(wanted>BUFFER-c->buffered)wanted=BUFFER-c->buffered;
        /* A worker exists only for r->channels <= FX_WORKER_MAX_CHANNELS. */
        float *pointers[FX_WORKER_MAX_CHANNELS];for(unsigned i=0;i<r->channels;++i)pointers[i]=c->buffer[i]+c->buffered;
        unsigned from_start=c->startup_count;if(from_start>wanted)from_start=wanted;
        unsigned first=c->startup_size-c->startup_read;if(first>from_start)first=from_start;
        for(unsigned i=0;i<r->channels;++i) {
            memcpy(pointers[i],c->startup[i]+c->startup_read,first*4);
            memcpy(pointers[i]+first,c->startup[i],(from_start-first)*4);
            pointers[i]+=from_start;
        }
        if(c->startup_size)c->startup_read=(c->startup_read+from_start)%c->startup_size;
        c->startup_count-=from_start;c->buffered+=from_start;
        unsigned pulled=fx_worker_pull(c->worker,pointers,wanted-from_start);
        c->buffered+=pulled;c->nominal_pulled+=pulled;
    }
    int nominal_done=c->eof&&(!c->worker||fx_worker_finished(c->worker));
    if((c->buffered<need||!startup_ready)&&!nominal_done&&!c->eof) {
        if(c->worker&&c->real==0&&!fx_worker_error(c->worker)) {
            if(startup_expired(c))return;
            for(unsigned i=0;i<r->channels;++i)memset(dst[i],0,capacity*4);
            c->lead+=capacity;*output=capacity;
            c->startup_input_delay+=capacity*(double)r->i_rate/(r->o_rate*c->rate);
#ifdef FX_SRC_VERIFY
            fx_verify_account(*input,capacity,c->rate,c->eof,c->prime!=0);
#endif
            if(!c->eof)c->fraction+=capacity*(double)r->i_rate/(r->o_rate*c->rate);
            return;
        }
        ++c->underruns;++c->total_underruns;
        { int worker_error=c->worker?fx_worker_error(c->worker):0;
          if(worker_error){c->error=worker_error;return;} }
        /* Transient supply shortfall with a healthy worker (stalled engine,
         * scheduling jitter, catch-up callbacks): conceal with silence and
         * keep the stream alive instead of wedging it permanently. */
        for(unsigned i=0;i<r->channels;++i)memset(dst[i],0,capacity*4);
        *output=capacity;
#ifdef FX_SRC_VERIFY
        fx_verify_account(*input,capacity,c->rate,c->eof,0);
#endif
        if(!c->eof)c->fraction+=capacity*(double)r->i_rate/(r->o_rate*c->rate);
        return;
    }
    if(nominal_done&&!c->flushed){c->flush=resample_delay(&c->match);c->flushed=1;}
    unsigned fresh=c->buffered,zeros=0;
    if(nominal_done&&fresh<need&&c->flush) {
        zeros=need-fresh;if(zeros>c->flush)zeros=c->flush;if(zeros>BUFFER-fresh)zeros=BUFFER-fresh;
        for(unsigned i=0;i<r->channels;++i)memset(c->buffer[i]+fresh,0,zeros*4);
        c->buffered+=zeros;c->flush-=zeros;
    }
    uint32_t take=c->buffered,made=capacity;
    resample_process(&c->match,(const void **)c->buffer,&take,dst,&made);
#ifdef FX_SRC_VERIFY
    fx_verify_match(c->rate,(const void **)c->buffer,c->buffered,capacity,take,made,dst);
    fx_verify_account(*input,made,c->rate,c->eof,0);
#endif
    if(take>c->buffered){c->error=EPROTO;return;}
    c->buffered-=take;
    for(unsigned i=0;i<r->channels;++i)memmove(c->buffer[i],c->buffer[i]+take,c->buffered*4);
    add_real(c,made);*output=made;
    if(!c->eof)c->fraction+=made*(double)r->i_rate/(r->o_rate*c->rate);
    if(nominal_done&&!c->flush&&!c->buffered&&!made)c->done=1;
}
int fx_resample_init(struct resample *r)
{
    enum fx_profile profile;
    int result=fx_src_load_profile(&profile);if(result<0)return result;
    struct composition *c=calloc(1,sizeof(*c));if(!c)return -ENOMEM;
    atomic_init(&c->startup_count,0);
    atomic_init(&c->buffered,0);atomic_init(&c->real,0);atomic_init(&c->lead,0);
    atomic_init(&c->reserve,0);atomic_init(&c->first_real_ns,0);
    atomic_init(&c->underruns,0);atomic_init(&c->error,0);
    /* Cross-thread diagnostics must not add locks to graph processing. */
    if(!atomic_is_lock_free(&c->startup_count)||!atomic_is_lock_free(&c->buffered)||!atomic_is_lock_free(&c->real)||!atomic_is_lock_free(&c->lead)||
       !atomic_is_lock_free(&c->reserve)||!atomic_is_lock_free(&c->first_real_ns)||
       !atomic_is_lock_free(&c->underruns)||!atomic_is_lock_free(&c->error)){free(c);return -ENOTSUP;}
#ifdef FX_SRC_VERIFY
    fx_verify_init(r);
#endif
    c->profile=profile;
    r->data=c;r->free=free_composition;r->update_rate=update_rate;r->in_len=input_length;r->out_len=output_length;
    r->process=process_composition;r->reset=reset_composition;r->delay=delay_frames;r->phase=delay_phase;
    r->func_name="fxroute-worker-plus-native-matching";
    c->match=(struct resample){.channels=r->channels,.i_rate=r->o_rate,.o_rate=r->o_rate,.quality=r->quality,
        .cpu_flags=r->cpu_flags,.log=r->log,.options=r->options,.config=r->config};
    if(resample_native_init(&c->match)<0)goto fail;
    c->buffer=calloc(r->channels,sizeof(float *));if(!c->buffer)goto fail;
    for(unsigned i=0;i<r->channels;++i){c->buffer[i]=calloc(BUFFER,4);if(!c->buffer[i])goto fail;}
    c->rate=c->rate_min=c->rate_max=1;
    if(r->i_rate!=r->o_rate) {
        if(!r->i_rate||!r->o_rate||r->i_rate>MAX_RATE||r->o_rate>MAX_RATE||
           r->channels>FX_WORKER_MAX_CHANNELS)goto fail;
        c->startup_size=(3ULL*r->o_rate+3)/4+BUFFER;
        c->startup=calloc(r->channels,sizeof(float *));if(!c->startup)goto fail;
        for(unsigned i=0;i<r->channels;++i){c->startup[i]=calloc(c->startup_size,4);if(!c->startup[i])goto fail;}
        c->worker=fx_worker_create(profile,r->channels,r->i_rate,r->o_rate,65536);if(!c->worker)goto fail;
        /* Initialization is outside the graph process callback. */
        struct fx_worker_stats stats;
        for(unsigned retry=0;retry<1000;++retry){fx_worker_stats(c->worker,&stats);
            if(stats.acknowledged==stats.generation)break;usleep(1000);}
        if(stats.acknowledged!=stats.generation||stats.error)goto fail;
        c->prime=ceil(.750*r->o_rate);
    }
    return 0;
fail:
    free_composition(r);return -EIO;
}
