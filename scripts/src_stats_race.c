/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <assert.h>
#include <dirent.h>
#include <dlfcn.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "resample-worker.h"

static struct resample resample;
static _Atomic int started,stopped;
static int (*error)(struct resample *);
static void (*stats)(struct resample *,struct fx_resample_stats *);
/* Snapshot invariants within one generation; latencies are write-once. */
static void check(const struct fx_resample_stats *s,struct fx_resample_stats *seen)
{
    assert(s->reserve_frames<=s->lead_frames);
    assert(!s->real_frames||s->first_real_ns);
    assert(!s->worker.produced||s->worker.first_output_ns);
    assert(s->lead_frames>=seen->lead_frames&&s->reserve_frames>=seen->reserve_frames&&s->real_frames>=seen->real_frames);
    assert(!seen->first_real_ns||s->first_real_ns==seen->first_real_ns);
    assert(!seen->worker.first_output_ns||s->worker.first_output_ns==seen->worker.first_output_ns);
    *seen=*s;
}
static void *drain(void *data)
{
    float output[2][256];void *dst[]={output[0],output[1]};
    atomic_store(&started,1);
    for(unsigned n=0;n<60000;++n) {
        unsigned take=0,made=1;resample_process(&resample,NULL,&take,dst,&made);
    }
    atomic_store(&stopped,1);
    return NULL;
}
static void *paced(void *data)
{
    static float input[2][512],output[2][512];
    for(unsigned i=0;i<512;++i)input[0][i]=input[1][i]=(i%64)/640.f;
    const void *src[]={input[0],input[1]};void *dst[]={output[0],output[1]};
    struct fx_resample_stats s={0};
    atomic_store(&started,1);
    for(unsigned n=0;n<1000&&s.real_frames<24000;++n) {
        unsigned take=512,made=512;resample_process(&resample,src,&take,dst,&made);
        assert(!error(&resample));stats(&resample,&s);usleep(10666);
    }
    atomic_store(&stopped,1);
    return NULL;
}
static void read_concurrently(void *(*writer)(void *))
{
    pthread_t thread;assert(pthread_create(&thread,NULL,writer,NULL)==0);
    while(!atomic_load(&started))usleep(100);
    struct fx_resample_stats seen={0};unsigned reads=0;
    while(!atomic_load(&stopped)){struct fx_resample_stats s;stats(&resample,&s);check(&s,&seen);++reads;}
    assert(pthread_join(thread,NULL)==0&&reads);
}
int main(int argc,char **argv)
{
    assert(argc==2||(argc==3&&!strcmp(argv[2],"startup")));
    void *library=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(library);
    int (*init)(struct resample *)=dlsym(library,"fx_resample_init");
    void (*finish)(struct resample *)=dlsym(library,"fx_resample_finish");
    error=dlsym(library,"fx_resample_error");stats=dlsym(library,"fx_resample_stats");
    assert(init&&finish&&error&&stats);
    resample=(struct resample){.channels=2,.i_rate=44100,.o_rate=48000,.quality=4};assert(!init(&resample));
    if(argc==3) {
        /* Startup telemetry is published race-free and cleared per generation. */
        read_concurrently(paced);
        struct fx_resample_stats s;stats(&resample,&s);
        assert(s.real_frames>=24000&&s.reserve_frames==36000&&s.lead_frames>=s.reserve_frames);
        assert(s.worker.first_output_ns&&s.first_real_ns>=s.worker.first_output_ns);
        resample_reset(&resample);
        do{stats(&resample,&s);usleep(1000);}while(s.worker.acknowledged!=s.worker.generation);
        assert(!s.lead_frames&&!s.reserve_frames&&!s.real_frames&&!s.first_real_ns&&!s.worker.first_output_ns&&!s.worker.produced);
        resample_free(&resample);dlclose(library);return 0;
    }
    float input[2][256]={{0}},output[2][256];const void *src[]={input[0],input[1]};void *dst[]={output[0],output[1]};
    unsigned take=256,made=256;resample_process(&resample,src,&take,dst,&made);assert(take&&made);
    DIR *tasks=opendir("/proc/self/task");assert(tasks);struct dirent *entry;int child=0;
    while((entry=readdir(tasks))&&!child) {
        char path[512];snprintf(path,sizeof(path),"/proc/self/task/%s/children",entry->d_name);
        FILE *file=fopen(path,"r");if(file){fscanf(file,"%d",&child);fclose(file);}
    }
    closedir(tasks);assert(child>0&&kill(child,SIGKILL)==0);
    finish(&resample);
    for(unsigned n=0;n<1000&&!error(&resample);++n)usleep(1000);assert(error(&resample));
    read_concurrently(drain);
    resample_free(&resample);dlclose(library);return 0;
}
