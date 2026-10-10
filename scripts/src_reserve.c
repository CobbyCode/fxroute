/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include <assert.h>
#include <dlfcn.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "resample-worker.h"

static void run(struct resample *r, const char *scenario, unsigned length,
    void (*stats)(struct resample *,struct fx_resample_stats *),
    void (*finish)(struct resample *), int (*finished)(struct resample *),
    int (*error)(struct resample *), double (*match_delay)(struct resample *))
{
    float source[2][512]={{0}},output[2][512];
    const void *input[]={source[0],source[1]};void *dest[]={output[0],output[1]};
    unsigned submitted=0,eof=0,started=0;double reserve=0,last_rate=1;
    struct fx_resample_stats before={0},after={0};
    for(unsigned tick=0;tick<4000&&!finished(r);++tick) {
        double rate=2;
        if(!strcmp(scenario,"2-1")&&tick>=100)rate=1;
        if(!strcmp(scenario,"1-2")&&tick<100)rate=1;
        if(!strcmp(scenario,"2-1-2")&&tick>=100&&tick<150)rate=1;
        if(!strcmp(scenario,"drift"))rate=tick%2?1.0005:.9995;
        last_rate=rate;resample_update_rate(r,rate);
        if(submitted==length&&!eof){finish(r);eof=1;}
        unsigned take=length-submitted;if(take>512)take=512;
        unsigned made=256;stats(r,&before);
        resample_process(r,input,&take,dest,&made);submitted+=take;stats(r,&after);
        assert(!error(r));
        unsigned lead=after.lead_frames-before.lead_frames;
        if(lead) {
            assert(!started&&"Startup padding must not resume after real PCM");
            reserve+=lead/rate;
            for(unsigned c=0;c<2;++c)for(unsigned i=0;i<made;++i)assert(!output[c][i]);
        }
        if(after.real_frames&&!started) {
            assert(reserve>=36000-1e-6&&reserve<36000+1/rate+1e-6);
            if(!eof)assert(submitted>=72000&&"Clock scaling must not shorten nominal input reserve");
            started=1;
        }
        if(after.real_frames&&!eof) {
            double full=(double)resample_delay(r)+resample_phase(r);
            /* Integrate padding at each clock rate, not at the current rate. */
            assert(fabs(match_delay(r)-fmax(0,full-reserve*2))<.001);
            assert(full>=reserve*2-256&&"Full timeline latency must retain startup padding");
        }
        usleep(256*1000000/(48000*rate));
    }
    assert(finished(r)&&submitted==length);
    if(length) {
        assert(started&&reserve>=36000-1e-6&&reserve<36000+1/last_rate+1e-6);
        assert(after.worker.produced==length/2&&after.worker.delivered==length/2);
    } else assert(!reserve&&!after.real_frames);
    fprintf(stderr,"Reserve %s: input=%u nominal_lead=%.9f real=%lu\n",scenario,submitted,reserve,after.real_frames);
}

int main(int argc,char **argv)
{
    assert(argc==3);void *lib=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(lib);
    int (*init)(struct resample *)=dlsym(lib,"fx_resample_init");
    void (*stats)(struct resample *,struct fx_resample_stats *)=dlsym(lib,"fx_resample_stats");
    void (*finish)(struct resample *)=dlsym(lib,"fx_resample_finish");
    int (*finished)(struct resample *)=dlsym(lib,"fx_resample_finished");
    int (*error)(struct resample *)=dlsym(lib,"fx_resample_error");
    double (*match_delay)(struct resample *)=dlsym(lib,"fx_resample_match_delay");
    assert(init&&stats&&finish&&finished&&error&&match_delay);
    struct resample r={.channels=2,.i_rate=96000,.o_rate=48000,.quality=4};assert(!init(&r));
    run(&r,argv[2],96000,stats,finish,finished,error,match_delay);
    resample_reset(&r);assert(!match_delay(&r));run(&r,argv[2],128,stats,finish,finished,error,match_delay);
    resample_reset(&r);assert(!match_delay(&r));run(&r,argv[2],0,stats,finish,finished,error,match_delay);
    resample_free(&r);dlclose(lib);return 0;
}
