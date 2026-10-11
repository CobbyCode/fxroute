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
{ struct timespec t; assert(!clock_gettime(CLOCK_MONOTONIC, &t)); return t.tv_sec+t.tv_nsec/1e9; }
static pid_t child(void)
{
    DIR *tasks=opendir("/proc/self/task"); assert(tasks); struct dirent *entry; int pid=0;
    while ((entry=readdir(tasks)) && !pid) {
        char path[512]; snprintf(path,sizeof path,"/proc/self/task/%s/children",entry->d_name);
        FILE *file=fopen(path,"r"); if(file){(void)fscanf(file,"%d",&pid);fclose(file);}
    }
    closedir(tasks); assert(pid>0); return pid;
}
int main(int argc,char **argv)
{
    assert(argc==7); void *library=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL); assert(library);
    int (*init)(struct resample *)=dlsym(library,"fx_resample_init");
    int (*error)(struct resample *)=dlsym(library,"fx_resample_error");
    int (*finished)(struct resample *)=dlsym(library,"fx_resample_finished");
    void (*finish)(struct resample *)=dlsym(library,"fx_resample_finish");
    void (*stats)(struct resample *,struct fx_resample_stats *)=dlsym(library,"fx_resample_stats");
    assert(init&&error&&finished&&finish&&stats);
    struct resample r={.channels=2,.i_rate=atoi(argv[2]),.o_rate=atoi(argv[3]),.quality=4}; assert(!init(&r));
    FILE *source=fopen(argv[4],"rb"),*target=fopen(argv[5],"wb");assert(source&&target);
    fseek(source,0,SEEK_END);unsigned frames=ftell(source)/8;rewind(source);
    float *raw=calloc(frames*2,4),*planes=calloc(frames*2,4);assert(raw&&planes);
    assert(fread(raw,8,frames,source)==frames);fclose(source);
    for(unsigned c=0;c<2;c++)for(unsigned i=0;i<frames;i++)planes[c*frames+i]=raw[2*i+c];free(raw);
    float output[2][1024];void *dst[]={output[0],output[1]};
    struct fx_resample_stats state={0};unsigned submitted=0,eof=0;
    int timeout=!strcmp(argv[6],"timeout");
    if(timeout)assert(!kill(child(),SIGSTOP));
    double start=seconds();
    while(!finished(&r)&&!error(&r)&&seconds()-start<8) {
        stats(&r,&state);
        if(timeout&&submitted){usleep(3900000);unsigned take=0,made=0;const void *src[]={planes,planes+frames};
            resample_process(&r,src,&take,dst,&made);break;}
        if(!eof&&submitted==frames){assert(state.real_frames&&"Real output must start before EOF");finish(&r);eof=1;}
        const void *src[]={planes+submitted,planes+frames+submitted};
        unsigned take=frames-submitted,wanted=resample_in_len(&r,1024);if(take>wanted)take=wanted;
        unsigned made=1024;resample_process(&r,src,&take,dst,&made);submitted+=take;
        struct fx_resample_stats after;stats(&r,&after);
        if(after.underruns!=state.underruns)fprintf(stderr,"UNDERRUN submitted=%u take=%u made=%u lead=%lu real=%lu buffered=%u produced=%lu delivered=%lu eof=%u\n",
            submitted,take,made,after.lead_frames,after.real_frames,after.buffered,after.worker.produced,after.worker.delivered,eof);
        for(unsigned i=0;i<made;i++)for(unsigned c=0;c<2;c++)assert(fwrite(&output[c][i],4,1,target)==1);
        usleep(!strcmp(argv[6],"fast")?1000:(unsigned)(1024000000ULL/r.o_rate));
    }
    stats(&r,&state);
    fprintf(stderr,"STARTUP_STATE accepted=%u lead=%lu real=%lu buffered=%u produced=%lu delivered=%lu error=%d eof=%u finished=%d\n",
            submitted,state.lead_frames,state.real_frames,state.buffered,state.worker.produced,state.worker.delivered,error(&r),eof,finished(&r));
    if(timeout)assert(error(&r)==ETIMEDOUT&&"Timeout must start with accepted input, before reserve completion");
    else {
        assert(!error(&r)&&finished(&r)&&submitted==frames);
        assert(state.real_frames&&state.reserve_frames==(uint64_t)((3ULL*r.o_rate+3)/4));
        assert(state.worker.accepted==frames&&state.worker.delivered==state.worker.produced);
        assert(!state.underruns&&"Healthy startup must preserve all PCM without concealment");
    }
    printf("HIGH_RATE_STARTUP {\"lead\":%lu,\"real\":%lu,\"accepted\":%lu,\"produced\":%lu,\"elapsed\":%.6f}\n",
           state.lead_frames,state.real_frames,state.worker.accepted,state.worker.produced,seconds()-start);
    fclose(target);resample_free(&r);dlclose(library);free(planes);return 0;
}
