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
#define STARTUP_WAIT_NS 3000000000ULL
static uint64_t startup_now_ns(void)
{ struct timespec now; clock_gettime(CLOCK_MONOTONIC,&now); return (uint64_t)now.tv_sec*1000000000+now.tv_nsec; }
struct composition {
    struct fx_worker *worker;
    struct resample match;
    float **buffer;
    _Atomic uint32_t buffered;
    uint32_t flush;
    uint64_t accepted, nominal_pulled;
    _Atomic uint64_t real, lead;
    double rate, fraction, prime, rate_min, rate_max;
    uint64_t rate_updates, max_callback_ns, callbacks;
    unsigned eof, flushed, done, total_underruns;
    _Atomic unsigned underruns;
    uint64_t startup_start_ns;
    unsigned startup_armed;
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
        "\"accepted\":%lu,\"produced\":%lu,\"delivered\":%lu,\"rate_min\":%.9f,\"rate_max\":%.9f,\"error\":%d}\n",
        fx_src_profile_name(c->profile),r->channels,r->i_rate,r->o_rate,stats.spawns,
        c->max_callback_ns/1e6,c->callbacks,c->total_underruns,stats.max_input,stats.max_output,
        stats.accepted,stats.produced,stats.delivered,c->rate_min,c->rate_max,c->error?c->error:stats.error);
    if(c->worker)fx_worker_destroy(c->worker);
    if(c->match.free)resample_free(&c->match);
    if(c->buffer)for(unsigned i=0;i<r->channels;++i)free(c->buffer[i]);
    free(c->buffer);free(c);r->data=NULL;
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
    double outstanding=c->accepted-(c->nominal_pulled-c->buffered)*(double)r->i_rate/r->o_rate;
    double delay=outstanding+((double)resample_delay(&c->match)+resample_phase(&c->match))*(double)r->i_rate/r->o_rate;
    return delay>0?delay:0;
}
static uint32_t delay_frames(struct resample *r) { return floor(timeline_delay(r)); }
static float delay_phase(struct resample *r) { double d=timeline_delay(r);return d-floor(d); }
static void reset_composition(struct resample *r)
{
    struct composition *c=r->data;
    if(c->worker)fx_worker_reset(c->worker);
    resample_reset(&c->match);
    c->buffered=c->flush=0;
    c->eof=c->flushed=c->done=c->underruns=0;c->error=0;
    c->startup_armed=0;c->startup_start_ns=0;
    c->accepted=c->nominal_pulled=c->real=c->lead=0;c->fraction=0;
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
    stats->real_frames=c->real;stats->lead_frames=c->lead;stats->underruns=c->underruns;
    stats->buffered=c->buffered;
}
void fx_resample_note_callback(struct resample *r,uint64_t ns)
{
    struct composition *c=r->data;
    if(ns>c->max_callback_ns)c->max_callback_ns=ns;
    ++c->callbacks;
}
/* Startup timeout is its own terminal error: it never replaces an existing
 * error and is not a queue underrun. */
static int startup_expired(struct composition *c)
{
    if(!c->worker||c->eof||c->error||!c->startup_armed||c->real||fx_worker_error(c->worker)||
       startup_now_ns()-c->startup_start_ns<=STARTUP_WAIT_NS)return 0;
    c->error=ETIMEDOUT;
    return 1;
}
static void process_composition(struct resample *r,const void * SPA_RESTRICT src[],uint32_t *input,
    void * SPA_RESTRICT dst[],uint32_t *output)
{
    struct composition *c=r->data;
    unsigned offered=*input,capacity=*output;*input=*output=0;
    if(c->done||c->error||(!capacity&&(c->worker||c->eof))) {
        startup_expired(c);
        return;
    }
    if(!c->eof) {
        unsigned take=offered;
        if(c->worker) {
            unsigned wanted=input_length(r,capacity);if(take>wanted)take=wanted;
            take=fx_worker_push(c->worker,(const float *const *)src,take);
            unsigned available=output_length(r,take);if(capacity>available)capacity=available;
            c->fraction-=take;
        } else {
            unsigned made=capacity;
            resample_process(&c->match,src,&take,dst,&made);
#ifdef FX_SRC_VERIFY
            fx_verify_match(c->rate,src,offered,capacity,take,made,dst);
            fx_verify_account(take,made,c->rate,0,0);
#endif
            *output=made;c->real+=made;c->nominal_pulled+=take;
        }
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
        c->prime=fmax(0,c->prime-made/c->rate);c->lead+=made;*output=made;
        if(c->prime==0&&!c->startup_armed){c->startup_armed=1;c->startup_start_ns=startup_now_ns();}
#ifdef FX_SRC_VERIFY
        fx_verify_account(*input,made,c->rate,c->eof,c->prime!=0);
#endif
        if(!c->eof)c->fraction+=made*(double)r->i_rate/(r->o_rate*c->rate);
        return;
    }
    unsigned need=resample_in_len(&c->match,capacity);
    if(c->worker&&c->buffered<need) {
        unsigned wanted=need-c->buffered;if(wanted>BUFFER-c->buffered)wanted=BUFFER-c->buffered;
        /* A worker exists only for r->channels <= FX_WORKER_MAX_CHANNELS. */
        float *pointers[FX_WORKER_MAX_CHANNELS];for(unsigned i=0;i<r->channels;++i)pointers[i]=c->buffer[i]+c->buffered;
        unsigned pulled=fx_worker_pull(c->worker,pointers,wanted);
        c->buffered+=pulled;c->nominal_pulled+=pulled;
    }
    int nominal_done=c->eof&&(!c->worker||fx_worker_finished(c->worker));
    if(c->buffered<need&&!nominal_done&&!c->eof) {
        if(c->worker&&c->real==0&&!fx_worker_error(c->worker)) {
            if(startup_expired(c))return;
            for(unsigned i=0;i<r->channels;++i)memset(dst[i],0,capacity*4);
            c->lead+=capacity;*output=capacity;
#ifdef FX_SRC_VERIFY
            fx_verify_account(*input,capacity,c->rate,c->eof,c->prime!=0);
#endif
            if(!c->eof)c->fraction+=capacity*(double)r->i_rate/(r->o_rate*c->rate);
            return;
        }
        ++c->underruns;++c->total_underruns;
        { int worker_error=c->worker?fx_worker_error(c->worker):0; c->error=worker_error?worker_error:EPIPE; }
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
    c->real+=made;*output=made;
    if(!c->eof)c->fraction+=made*(double)r->i_rate/(r->o_rate*c->rate);
    if(nominal_done&&!c->flush&&!c->buffered&&!made)c->done=1;
}
int fx_resample_init(struct resample *r)
{
    enum fx_profile profile;
    int result=fx_src_load_profile(&profile);if(result<0)return result;
    struct composition *c=calloc(1,sizeof(*c));if(!c)return -ENOMEM;
    atomic_init(&c->buffered,0);atomic_init(&c->real,0);atomic_init(&c->lead,0);
    atomic_init(&c->underruns,0);atomic_init(&c->error,0);
    /* Cross-thread diagnostics must not add locks to graph processing. */
    if(!atomic_is_lock_free(&c->buffered)||!atomic_is_lock_free(&c->real)||!atomic_is_lock_free(&c->lead)||
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
