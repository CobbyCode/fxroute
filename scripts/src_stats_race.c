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
#include <unistd.h>
#include "resample-worker.h"

static struct resample resample;
static _Atomic int started;
static void *drain(void *data)
{
    float output[2][256];void *dst[]={output[0],output[1]};
    atomic_store(&started,1);
    for(unsigned n=0;n<60000;++n) {
        unsigned take=0,made=1;resample_process(&resample,NULL,&take,dst,&made);
    }
    return NULL;
}
int main(int argc,char **argv)
{
    assert(argc==2);void *library=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(library);
    int (*init)(struct resample *)=dlsym(library,"fx_resample_init");
    void (*finish)(struct resample *)=dlsym(library,"fx_resample_finish");
    int (*error)(struct resample *)=dlsym(library,"fx_resample_error");
    void (*stats)(struct resample *,struct fx_resample_stats *)=dlsym(library,"fx_resample_stats");
    assert(init&&finish&&error&&stats);
    resample=(struct resample){.channels=2,.i_rate=44100,.o_rate=48000,.quality=4};assert(!init(&resample));
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
    pthread_t thread;assert(pthread_create(&thread,NULL,drain,NULL)==0);
    while(!atomic_load(&started))usleep(100);
    for(unsigned n=0;n<60000;++n){struct fx_resample_stats s;stats(&resample,&s);assert(s.lead_frames>=256);}
    assert(pthread_join(thread,NULL)==0);resample_free(&resample);dlclose(library);return 0;
}
