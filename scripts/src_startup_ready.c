/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include <assert.h>
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "resample-worker.h"

static double seconds(void)
{struct timespec now;assert(!clock_gettime(CLOCK_MONOTONIC,&now));return now.tv_sec+now.tv_nsec/1e9;}
static pid_t child(void)
{
    DIR *tasks=opendir("/proc/self/task");assert(tasks);struct dirent *entry;int pid=0;
    while((entry=readdir(tasks))&&!pid) {
        char path[512];snprintf(path,sizeof(path),"/proc/self/task/%s/children",entry->d_name);
        FILE *file=fopen(path,"r");if(file){fscanf(file,"%d",&pid);fclose(file);}
    }
    closedir(tasks);assert(pid>0);return pid;
}
int main(int argc,char **argv)
{
    assert(argc==5);void *library=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(library);
    int (*init)(struct resample *)=dlsym(library,"fx_resample_init");
    void (*finish)(struct resample *)=dlsym(library,"fx_resample_finish");
    int (*finished)(struct resample *)=dlsym(library,"fx_resample_finished");
    int (*error)(struct resample *)=dlsym(library,"fx_resample_error");
    void (*stats)(struct resample *,struct fx_resample_stats *)=dlsym(library,"fx_resample_stats");
    assert(init&&finish&&finished&&error&&stats);
    struct resample r={.channels=2,.i_rate=44100,.o_rate=48000,.quality=4};assert(!init(&r));
    FILE *source=fopen(argv[3],"rb"),*target=fopen(argv[4],"wb");assert(source&&target);
    fseek(source,0,SEEK_END);unsigned frames=ftell(source)/8;rewind(source);assert(frames==176400);
    float *raw=calloc(frames*2,4),*planes=calloc(frames*2,4);assert(raw&&planes);
    assert(fread(raw,8,frames,source)==frames);fclose(source);
    for(unsigned c=0;c<2;++c)for(unsigned i=0;i<frames;++i)planes[c*frames+i]=raw[i*2+c];free(raw);
    float output[2][512];void *dst[]={output[0],output[1]};
    pid_t pid=child();
    int pause_start=strcmp(argv[2],"post-start")!=0;
    if(pause_start)assert(!kill(pid,SIGSTOP));
    if(!strcmp(argv[2],"timeout-reset")) {
        double start=seconds();unsigned submitted=0;
        while(!error(&r)&&seconds()-start<5) {
            const void *src[]={planes+submitted,planes+frames+submitted};unsigned take=512,made=512;
            resample_process(&r,src,&take,dst,&made);submitted+=take;usleep(10666);
        }
        assert(error(&r)==ETIMEDOUT&&seconds()-start>=3.5&&seconds()-start<4.5);
        assert(submitted<frames&&"A full input FIFO must not disable the startup timeout");
        struct fx_resample_stats state;
        for(unsigned n=0;n<4;++n) {
            const void *src[]={planes,planes+frames};unsigned take=n&1?512:0,made=n&1?512:0;
            resample_process(&r,src,&take,dst,&made);assert(!made);
        }
        stats(&r,&state);
        assert(error(&r)==ETIMEDOUT&&!state.underruns&&"A startup timeout is neither rearmed nor an underrun");
        resample_reset(&r);
        do{stats(&r,&state);usleep(1000);}while(state.worker.acknowledged!=state.worker.generation);
        assert(!error(&r));pid=child();assert(!kill(pid,SIGSTOP));
    }
    double start=seconds(),first_real=0;unsigned submitted=0,eof=0,resumed=!pause_start,paused_late=0;
    struct fx_resample_stats before={0},after={0};
    while(!finished(&r)&&seconds()-start<10) {
        if(pause_start&&!resumed&&seconds()-start>=1.05){assert(!kill(pid,SIGCONT));resumed=1;}
        if(submitted==frames&&!eof){finish(&r);eof=1;}
        const void *src[]={planes+submitted,planes+frames+submitted};
        unsigned take=frames-submitted;if(take>512)take=512;unsigned made=512;
        stats(&r,&before);resample_process(&r,src,&take,dst,&made);submitted+=take;stats(&r,&after);
        if(error(&r)) {
            assert(!strcmp(argv[2],"post-start")&&paused_late&&error(&r)==EPIPE);
            assert(after.lead_frames==before.lead_frames);break;
        }
        assert(!before.real_frames||after.lead_frames==before.lead_frames);
        if(after.real_frames&&!first_real){first_real=seconds()-start;assert(!pause_start||first_real>=1.05);}
        for(unsigned i=0;i<made;++i)for(unsigned c=0;c<2;++c)assert(fwrite(&output[c][i],4,1,target)==1);
        if(!strcmp(argv[2],"post-start")&&!paused_late&&after.real_frames>=12000){assert(!kill(pid,SIGSTOP));paused_late=1;}
        usleep(10666);
    }
    if(!strcmp(argv[2],"post-start"))assert(error(&r)==EPIPE&&first_real);
    else assert(finished(&r)&&submitted==frames&&!error(&r)&&after.worker.delivered==192000&&after.lead_frames>36000);
    fprintf(stdout,"STARTUP_READY {\"lead\":%lu,\"first_real_s\":%.6f}\n",after.lead_frames,first_real);
    fclose(target);resample_free(&r);dlclose(library);free(planes);return 0;
}
