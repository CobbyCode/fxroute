/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include <assert.h>
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <math.h>
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
    double (*match_delay)(struct resample *)=dlsym(library,"fx_resample_match_delay");
    assert(init&&finish&&finished&&error&&stats&&match_delay);
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
        assert(!state.worker.first_output_ns&&!state.first_real_ns&&state.lead_frames>state.reserve_frames&&
               "A startup that never became ready shows no engine output, only extended silence");
        resample_reset(&r);
        assert(!match_delay(&r));
        do{stats(&r,&state);usleep(1000);}while(state.worker.acknowledged!=state.worker.generation);
        assert(!state.worker.first_output_ns&&!state.first_real_ns&&!state.lead_frames&&!state.reserve_frames);
        assert(!error(&r));pid=child();assert(!kill(pid,SIGSTOP));
    }
    double start=seconds(),first_real=0,stall_start=0;unsigned submitted=0,eof=0,resumed=!pause_start,paused_late=0,resumed_late=0;
    struct fx_resample_stats before={0},after={0};
    while(!finished(&r)&&seconds()-start<10) {
        if(pause_start&&!resumed&&seconds()-start>=1.05){assert(!kill(pid,SIGCONT));resumed=1;}
        if(submitted==frames&&!eof){finish(&r);eof=1;}
        const void *src[]={planes+submitted,planes+frames+submitted};
        unsigned take=frames-submitted;if(take>512)take=512;unsigned made=512;
        stats(&r,&before);resample_process(&r,src,&take,dst,&made);submitted+=take;stats(&r,&after);
        if(!strcmp(argv[2],"post-start")&&paused_late&&!resumed_late&&seconds()-stall_start>=3.0) {
            /* A stalled healthy engine is concealed with counted silence and
             * resumes real output; only a dead engine may fail. */
            assert(!error(&r)&&after.underruns>=5);
            assert(after.lead_frames==before.lead_frames);
            assert(!kill(pid,SIGCONT));resumed_late=1;
        }
        if(error(&r)) {
            assert(0&&"post-start stalls must be concealed with silence, never terminal");
        }
        assert(!before.real_frames||after.lead_frames==before.lead_frames);
        if(after.real_frames&&!eof) {
            double full=(double)resample_delay(&r)+resample_phase(&r);
            double fixed=(double)after.lead_frames*44100/48000;
            assert(fabs(match_delay(&r)-fmax(0,full-fixed))<.001&&
                   "Clock feedback must exclude readiness extensions as well as the minimum reserve");
            assert(full>=fixed-512&&"Readiness extensions remain in full processing latency");
        }
        if(after.real_frames&&!first_real){first_real=seconds()-start;assert(!pause_start||first_real>=1.05);}
        for(unsigned i=0;i<made;++i)for(unsigned c=0;c<2;++c)assert(fwrite(&output[c][i],4,1,target)==1);
        if(!strcmp(argv[2],"post-start")&&!paused_late&&after.real_frames>=12000){assert(!kill(pid,SIGSTOP));paused_late=1;stall_start=seconds();}
        usleep(10666);
    }
    if(!strcmp(argv[2],"post-start"))assert(!error(&r)&&first_real&&resumed_late&&after.underruns>=5&&finished(&r));
    else assert(finished(&r)&&submitted==frames&&!error(&r)&&after.worker.delivered==192000&&after.lead_frames>36000);
    /* Product telemetry: starvation after real output keeps both latencies,
     * a delayed engine shows up in first_output_ns, and first_real_ns agrees
     * with this harness's independent clock. */
    assert(after.reserve_frames==36000&&after.lead_frames>=after.reserve_frames);
    assert(after.worker.first_output_ns&&after.first_real_ns>=after.worker.first_output_ns&&after.first_real_ns>=700000000);
    assert(fabs(after.first_real_ns/1e9-first_real)<.05);
    if(pause_start)assert(after.worker.first_output_ns>=1000000000&&after.lead_frames>after.reserve_frames);
    fprintf(stdout,"STARTUP_READY {\"lead\":%lu,\"reserve\":%lu,\"first_output_ns\":%lu,\"first_real_ns\":%lu,\"first_real_s\":%.6f}\n",
            after.lead_frames,after.reserve_frames,after.worker.first_output_ns,after.first_real_ns,first_real);
    fclose(target);resample_free(&r);dlclose(library);free(planes);return 0;
}
