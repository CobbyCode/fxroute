/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <assert.h>
#include <dlfcn.h>
#include <math.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>
#include <spa/param/audio/raw.h>
#include "resample-worker.h"

#define SPA_CHANNELS SPA_AUDIO_MAX_CHANNELS
static float input[SPA_CHANNELS][512],output[SPA_CHANNELS][512];
static double seconds(void)
{struct timespec now;assert(!clock_gettime(CLOCK_MONOTONIC,&now));return now.tv_sec+now.tv_nsec/1e9;}
int main(int argc,char **argv)
{
    assert(argc==2);void *library=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(library);
    int (*init)(struct resample *)=dlsym(library,"fx_resample_init");
    int (*error)(struct resample *)=dlsym(library,"fx_resample_error");
    void (*stats)(struct resample *,struct fx_resample_stats *)=dlsym(library,"fx_resample_stats");
    assert(init&&error&&stats);
    const void *src[SPA_CHANNELS];void *dst[SPA_CHANNELS];
    for(unsigned c=0;c<SPA_CHANNELS;++c){src[c]=input[c];dst[c]=output[c];for(unsigned i=0;i<512;++i)input[c][i]=.01f*(c+1);}
    /* Above the worker limit an unequal-rate adapter is rejected before processing. */
    unsigned over[]={FX_WORKER_MAX_CHANNELS+1,SPA_CHANNELS};
    for(unsigned k=0;k<2;++k) {
        struct resample r={.channels=over[k],.i_rate=44100,.o_rate=48000,.quality=4};
        assert(init(&r)<0&&!r.data);
    }
    /* Equal-rate adapters up to the SPA maximum run without a worker. */
    for(unsigned k=0;k<2;++k) {
        struct resample r={.channels=over[k],.i_rate=48000,.o_rate=48000,.quality=4};assert(!init(&r));
        unsigned made_total=0;
        for(unsigned n=0;n<8;++n){unsigned take=512,made=512;resample_process(&r,src,&take,dst,&made);made_total+=made;}
        assert(!error(&r)&&made_total);resample_free(&r);
    }
    /* At the limit the worker pull path fills every channel from its own plane. */
    struct resample r={.channels=FX_WORKER_MAX_CHANNELS,.i_rate=44100,.o_rate=48000,.quality=4};assert(!init(&r));
    struct fx_resample_stats state={0};double start=seconds();unsigned made=0;
    while(state.real_frames<24000&&seconds()-start<10) {
        unsigned take=512;made=512;resample_process(&r,src,&take,dst,&made);
        assert(!error(&r));stats(&r,&state);usleep(1000);
    }
    assert(state.real_frames>=24000&&made);
    for(unsigned c=0;c<FX_WORKER_MAX_CHANNELS;++c)assert(fabsf(output[c][made-1]-.01f*(c+1))<1e-3f);
    fprintf(stdout,"CHANNEL_BOUNDS {\"limit\":%u,\"real\":%lu}\n",FX_WORKER_MAX_CHANNELS,state.real_frames);
    resample_free(&r);dlclose(library);return 0;
}
