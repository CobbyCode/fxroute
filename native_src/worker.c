/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <stdatomic.h>
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <sys/eventfd.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <soxr.h>
#include "worker.h"

#define BLOCK 4096
static char sox_binary[PATH_MAX];
static int sox_path_error;

/* Bind the mapped generation, not the mutable pathname passed to dlopen. */
__attribute__((constructor)) static void resolve_sox_binary(void)
{
    Dl_info info;
    sox_path_error=ENOENT;
    if(!dladdr((void *)resolve_sox_binary,&info))return;
    FILE *maps=fopen("/proc/self/maps","r");
    if(!maps){sox_path_error=errno;return;}
    char *line=NULL;size_t capacity=0;
    while(getline(&line,&capacity,maps)>=0) {
        unsigned long start,end;int offset=0;
        if(sscanf(line,"%lx-%lx %*s %*s %*s %*s %n",&start,&end,&offset)!=2||!offset)continue;
        unsigned long address=(unsigned long)info.dli_fbase;
        if(address<start||address>=end)continue;
        line[strcspn(line,"\n")]=0;
        if(realpath(line+offset,sox_binary))sox_path_error=0;
        else sox_path_error=errno;
        break;
    }
    free(line);fclose(maps);
    if(sox_path_error)return;
    char *name=strrchr(sox_binary,'/');
    if(!name||(size_t)(name+1-sox_binary)+sizeof("sox")>sizeof(sox_binary)) {
        sox_path_error=ENAMETOOLONG;
        return;
    }
    strcpy(name+1,"sox");
}
struct ring { float *data; _Atomic uint64_t read, write; };
struct fx_worker {
    enum fx_profile profile;
    unsigned channels, ir, ore, capacity;
    struct ring input, output;
    pthread_t thread;
    int wake, bypass, thread_started;
    _Atomic unsigned generation, acknowledged, eof, done, stop, spawns;
    _Atomic int error;
    _Atomic uint64_t accepted, produced, delivered, max_input, max_output, first_input_ns, first_output_ns;
    float *in_stage, *out_stage;
};
static uint64_t available(struct ring *r)
{ return atomic_load_explicit(&r->write,memory_order_acquire)-atomic_load_explicit(&r->read,memory_order_acquire); }
static void notify(struct fx_worker *w)
{ uint64_t one=1; ssize_t result=write(w->wake,&one,sizeof(one)); (void)result; }
static uint64_t now_ns(void)
{ struct timespec now; clock_gettime(CLOCK_MONOTONIC,&now); return (uint64_t)now.tv_sec*1000000000+now.tv_nsec; }
/* Written once per generation by the single producer, before its frames
 * and the produced counter are published. */
static void note_output(struct fx_worker *w,unsigned frames)
{
    uint64_t start=atomic_load(&w->first_input_ns);
    if(frames&&start&&!atomic_load(&w->first_output_ns)){uint64_t ns=now_ns()-start;atomic_store(&w->first_output_ns,ns?ns:1);}
}
static int ready(struct fx_worker *w)
{ return atomic_load_explicit(&w->generation,memory_order_acquire)==atomic_load_explicit(&w->acknowledged,memory_order_acquire); }
static void clear_ring(struct ring *r)
{ atomic_store(&r->read,0); atomic_store(&r->write,0); }
static void clear_generation(struct fx_worker *w)
{
    clear_ring(&w->input); clear_ring(&w->output);
    atomic_store(&w->accepted,0); atomic_store(&w->produced,0); atomic_store(&w->delivered,0);
    atomic_store(&w->done,0); atomic_store(&w->error,0);
    atomic_store(&w->first_input_ns,0); atomic_store(&w->first_output_ns,0);
}
static unsigned ring_read(struct fx_worker *w,struct ring *r,float *dst,unsigned frames)
{
    uint64_t pos=atomic_load_explicit(&r->read,memory_order_relaxed),count=available(r);
    if(frames>count)frames=count;
    unsigned first=w->capacity-pos%w->capacity; if(first>frames)first=frames;
    memcpy(dst,r->data+(pos%w->capacity)*w->channels,first*w->channels*4);
    memcpy(dst+first*w->channels,r->data,(frames-first)*w->channels*4);
    atomic_store_explicit(&r->read,pos+frames,memory_order_release);
    return frames;
}
static unsigned ring_write(struct fx_worker *w,struct ring *r,const float *src,unsigned frames)
{
    uint64_t pos=atomic_load_explicit(&r->write,memory_order_relaxed),room=w->capacity-available(r);
    if(frames>room)frames=room;
    unsigned first=w->capacity-pos%w->capacity; if(first>frames)first=frames;
    memcpy(r->data+(pos%w->capacity)*w->channels,src,first*w->channels*4);
    memcpy(r->data,src+first*w->channels,(frames-first)*w->channels*4);
    if(r==&w->output)note_output(w,frames);
    atomic_store_explicit(&r->write,pos+frames,memory_order_release);
    if(r==&w->output) {
        atomic_fetch_add(&w->produced,frames);
        uint64_t used=available(r); if(used>atomic_load(&w->max_output))atomic_store(&w->max_output,used);
    }
    return frames;
}
unsigned fx_worker_push(struct fx_worker *w,const float *const *src,unsigned frames)
{
    if(!ready(w)||atomic_load(&w->eof)==atomic_load(&w->generation)||atomic_load(&w->error))return 0;
    struct ring *r=w->bypass?&w->output:&w->input;
    uint64_t pos=atomic_load_explicit(&r->write,memory_order_relaxed),room=w->capacity-available(r);
    if(frames>room)frames=room;
    for(unsigned i=0;i<frames;++i)for(unsigned c=0;c<w->channels;++c)
        r->data[((pos+i)%w->capacity)*w->channels+c]=src[c][i];
    /* Published with the frames, so the producer always sees the start time. */
    if(frames&&!atomic_load_explicit(&w->first_input_ns,memory_order_relaxed))atomic_store(&w->first_input_ns,now_ns());
    if(w->bypass)note_output(w,frames);
    atomic_store_explicit(&r->write,pos+frames,memory_order_release);
    atomic_fetch_add(&w->accepted,frames);
    uint64_t used=available(r);
    if(w->bypass) {
        atomic_fetch_add(&w->produced,frames);
        if(used>atomic_load(&w->max_output))atomic_store(&w->max_output,used);
    } else if(used>atomic_load(&w->max_input))atomic_store(&w->max_input,used);
    if(frames&&!w->bypass)notify(w);
    return frames;
}
unsigned fx_worker_pull(struct fx_worker *w,float *const *dst,unsigned frames)
{
    if(!ready(w))return 0;
    struct ring *r=&w->output;
    uint64_t pos=atomic_load_explicit(&r->read,memory_order_relaxed),count=available(r);
    if(frames>count)frames=count;
    for(unsigned i=0;i<frames;++i)for(unsigned c=0;c<w->channels;++c)
        dst[c][i]=r->data[((pos+i)%w->capacity)*w->channels+c];
    atomic_store_explicit(&r->read,pos+frames,memory_order_release);
    atomic_fetch_add(&w->delivered,frames);
    if(frames&&!w->bypass)notify(w);
    return frames;
}
unsigned fx_worker_available(struct fx_worker *w) { return ready(w)?available(&w->output):0; }
void fx_worker_finish(struct fx_worker *w)
{
    unsigned generation=atomic_load(&w->generation);
    atomic_store_explicit(&w->eof,generation,memory_order_release);
    if(w->bypass)atomic_store(&w->done,generation); else notify(w);
}
void fx_worker_reset(struct fx_worker *w)
{
    atomic_store(&w->eof,0);
    unsigned generation=atomic_fetch_add_explicit(&w->generation,1,memory_order_acq_rel)+1;
    if(w->bypass){clear_generation(w); atomic_store(&w->acknowledged,generation);} else notify(w);
}
int fx_worker_finished(struct fx_worker *w)
{ return ready(w)&&atomic_load(&w->done)==atomic_load(&w->generation)&&!available(&w->output); }
int fx_worker_error(struct fx_worker *w) { return ready(w)?atomic_load(&w->error):0; }
void fx_worker_stats(struct fx_worker *w,struct fx_worker_stats *s)
{
    /* produced first: within a generation produced frames imply a latency. */
    uint64_t produced=atomic_load(&w->produced);
    *s=(struct fx_worker_stats){.accepted=atomic_load(&w->accepted),.produced=produced,
        .delivered=atomic_load(&w->delivered),.max_input=atomic_load(&w->max_input),.max_output=atomic_load(&w->max_output),
        .first_output_ns=atomic_load(&w->first_output_ns),
        .generation=atomic_load(&w->generation),.acknowledged=atomic_load(&w->acknowledged),
        .spawns=atomic_load(&w->spawns),.error=atomic_load(&w->error)};
}
struct child { pid_t pid; int in,out,err,ended,status; };
static void close_fd(int *fd) { if(*fd>=0)close(*fd); *fd=-1; }
static int spawn_sox(struct fx_worker *w,struct child *child)
{
    if(sox_path_error)return sox_path_error;
    int input[2],output[2],error[2];
    if(pipe2(input,O_CLOEXEC)<0)return errno;
    if(pipe2(output,O_CLOEXEC)<0){close(input[0]);close(input[1]);return errno;}
    if(pipe2(error,O_CLOEXEC)<0){close(input[0]);close(input[1]);close(output[0]);close(output[1]);return errno;}
    posix_spawn_file_actions_t actions; posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions,input[0],0);
    posix_spawn_file_actions_adddup2(&actions,output[1],1);
    posix_spawn_file_actions_adddup2(&actions,error[1],2);
    int fds[]={input[0],input[1],output[0],output[1],error[0],error[1]};
    for(unsigned i=0;i<6;++i)posix_spawn_file_actions_addclose(&actions,fds[i]);
    char ir[16],ore[16],ch[16]; snprintf(ir,sizeof(ir),"%u",w->ir); snprintf(ore,sizeof(ore),"%u",w->ore); snprintf(ch,sizeof(ch),"%u",w->channels);
    const char *binary=sox_binary;
    char *argv[40]={(char *)binary,"--buffer","512","-D","-L","-t","raw","-r",ir,"-e","floating-point","-b","32","-c",ch,"-",
        "-t","raw","-r",ore,"-e","floating-point","-b","32","-","rate"};
    unsigned n=26;
    if(w->profile==FX_STANDARD){argv[n++]="-M";argv[n++]="-v";argv[n++]="-s";}
    else {char *high[]={"-E","-z","-a","-B","99.5","-A","99.5","-f",ore};for(unsigned i=0;i<9;++i)argv[n++]=high[i];}
    argv[n]=NULL;
    extern char **environ;
    unsigned count=0;while(environ[count])++count;
    char **environment=calloc(count+1,sizeof(char *));
    int result=ENOMEM;
    if(environment) {
        unsigned kept=0;
        for(unsigned i=0;i<count;++i)
            if(strncmp(environ[i],"SOX_OPTS=",9))environment[kept++]=environ[i];
        result=posix_spawn(&child->pid,binary,&actions,NULL,argv,environment);
        free(environment);
    }
    posix_spawn_file_actions_destroy(&actions);
    close(input[0]);close(output[1]);close(error[1]);
    if(result){close(input[1]);close(output[0]);close(error[0]);return result;}
    child->in=input[1];child->out=output[0];child->err=error[0];
    fcntl(child->in,F_SETFL,O_NONBLOCK);fcntl(child->out,F_SETFL,O_NONBLOCK);fcntl(child->err,F_SETFL,O_NONBLOCK);
    atomic_fetch_add(&w->spawns,1);
    return 0;
}
static void stop_child(struct child *child)
{
    close_fd(&child->in);close_fd(&child->out);close_fd(&child->err);
    if(child->pid>0&&!child->ended){kill(child->pid,SIGKILL);while(waitpid(child->pid,&child->status,0)<0&&errno==EINTR){} }
    child->pid=0;
}
static void *worker_loop(void *object)
{
    struct fx_worker *w=object;
    sigset_t blocked;sigemptyset(&blocked);sigaddset(&blocked,SIGPIPE);pthread_sigmask(SIG_BLOCK,&blocked,NULL);
    unsigned generation=0;
    struct child child={.in=-1,.out=-1,.err=-1};
    soxr_t low=NULL;
    size_t input_size=0,input_pos=0,output_size=0;
    int output_eof=0;
    for(;;) {
        if(atomic_load(&w->stop))break;
        unsigned current=atomic_load_explicit(&w->generation,memory_order_acquire);
        if(current!=generation) {
            stop_child(&child);if(low)soxr_delete(low);low=NULL;
            clear_generation(w);input_size=input_pos=output_size=0;output_eof=0;
            child=(struct child){.in=-1,.out=-1,.err=-1};generation=current;
            int error=0;
            if(w->profile==FX_LOW) {
                soxr_error_t problem;
                soxr_io_spec_t io=soxr_io_spec(SOXR_FLOAT32_I,SOXR_FLOAT32_I);
                soxr_quality_spec_t quality=soxr_quality_spec(SOXR_VHQ,0);
                soxr_runtime_spec_t runtime=soxr_runtime_spec(1);
                low=soxr_create(w->ir,w->ore,w->channels,&problem,&io,&quality,&runtime);
                if(!low||problem)error=EINVAL;
            } else error=spawn_sox(w,&child);
            atomic_store(&w->error,error);
            atomic_store_explicit(&w->acknowledged,generation,memory_order_release);
        }
        if(!atomic_load(&w->error)&&atomic_load(&w->done)!=generation) {
            unsigned stride=w->channels*4;
            if(input_pos==input_size&&available(&w->input)) {
                unsigned frames=ring_read(w,&w->input,w->in_stage,BLOCK);
                input_size=frames*stride;input_pos=0;
            }
            unsigned room=w->capacity-available(&w->output);
            int eof=atomic_load_explicit(&w->eof,memory_order_acquire)==generation&&!available(&w->input)&&input_pos==input_size;
            if(low) {
                if(room&&(input_pos<input_size||eof)) {
                    size_t taken=0,made=0;
                    unsigned cap=room<BLOCK?room:BLOCK;
                    soxr_error_t error=soxr_process(low,eof?NULL:(char *)w->in_stage+input_pos,(input_size-input_pos)/stride,&taken,
                        w->out_stage,cap,&made);
                    if(error)atomic_store(&w->error,EIO);
                    input_pos+=taken*stride;
                    ring_write(w,&w->output,w->out_stage,made);
                    if(eof&&!made)atomic_store(&w->done,generation);
                }
            } else {
                if(input_pos<input_size&&child.in>=0) {
                    ssize_t n=write(child.in,(char *)w->in_stage+input_pos,input_size-input_pos);
                    if(n>0)input_pos+=n;
                    else if(n<0&&errno!=EAGAIN&&errno!=EINTR)atomic_store(&w->error,errno);
                }
                if(eof)close_fd(&child.in);
                if(room&&child.out>=0) {
                    size_t cap=(room<BLOCK?room:BLOCK)*stride;
                    ssize_t n=read(child.out,(char *)w->out_stage+output_size,cap-output_size);
                    if(n>0)output_size+=n;
                    else if(!n){output_eof=1;close_fd(&child.out);}
                    else if(errno!=EAGAIN&&errno!=EINTR)atomic_store(&w->error,errno);
                    unsigned complete=output_size/stride;
                    if(complete){ring_write(w,&w->output,w->out_stage,complete);output_size-=complete*stride;
                        memmove(w->out_stage,(char *)w->out_stage+complete*stride,output_size);}
                }
                if(child.err>=0){char diagnostic[2048];ssize_t n=read(child.err,diagnostic,sizeof(diagnostic));if(!n)close_fd(&child.err);}
                if(child.pid>0&&!child.ended&&waitpid(child.pid,&child.status,WNOHANG)==child.pid)child.ended=1;
                if(child.ended&&(!WIFEXITED(child.status)||WEXITSTATUS(child.status)))atomic_store(&w->error,EIO);
                if(child.ended&&output_eof){if(output_size)atomic_store(&w->error,EPROTO);else atomic_store(&w->done,generation);}
            }
        }
        struct pollfd fds[4]={{w->wake,POLLIN,0},{-1,POLLOUT,0},{-1,POLLIN,0},{-1,POLLIN,0}};
        if(!atomic_load(&w->error)&&atomic_load(&w->done)!=generation){
            if(input_pos<input_size)fds[1].fd=child.in;
            if(w->capacity>available(&w->output))fds[2].fd=child.out;
            fds[3].fd=child.err;
        }
        poll(fds,4,low?1:5);
        if(fds[0].revents&POLLIN){uint64_t count;ssize_t n=read(w->wake,&count,sizeof(count));(void)n;}
    }
    stop_child(&child);if(low)soxr_delete(low);
    return NULL;
}
struct fx_worker *fx_worker_create(enum fx_profile p,unsigned channels,unsigned ir,unsigned ore,unsigned capacity)
{
    if(p>FX_HIGH||!channels||channels>FX_WORKER_MAX_CHANNELS||!ir||!ore||capacity<256||capacity>262144){errno=EINVAL;return NULL;}
    struct fx_worker *w=calloc(1,sizeof(*w));if(!w)return NULL;
    w->profile=p;w->channels=channels;w->ir=ir;w->ore=ore;w->capacity=capacity;w->bypass=ir==ore;w->wake=-1;
    w->input.data=calloc(capacity*channels,4);w->output.data=calloc(capacity*channels,4);
    w->in_stage=calloc(BLOCK*channels+32,4);w->out_stage=calloc(BLOCK*channels+32,4);
    if(!w->input.data||!w->output.data||!w->in_stage||!w->out_stage)goto fail;
    atomic_store(&w->generation,1);
    if(w->bypass){atomic_store(&w->acknowledged,1);return w;}
    w->wake=eventfd(0,EFD_NONBLOCK|EFD_CLOEXEC);if(w->wake<0)goto fail;
    if(pthread_create(&w->thread,NULL,worker_loop,w))goto fail;
    w->thread_started=1;
    return w;
fail:
    fx_worker_destroy(w);return NULL;
}
void fx_worker_destroy(struct fx_worker *w)
{
    if(!w)return;
    if(w->thread_started){atomic_store(&w->stop,1);notify(w);pthread_join(w->thread,NULL);}
    close_fd(&w->wake);free(w->input.data);free(w->output.data);free(w->in_stage);free(w->out_stage);free(w);
}
