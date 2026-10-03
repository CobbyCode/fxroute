/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <assert.h>
#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <spa/node/node.h>
#include <spa/node/io.h>
#include <spa/node/command.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/props.h>
#include <spa/support/loop.h>
#include <spa/support/plugin.h>

static struct spa_source source;
static spa_source_event_func_t event;
static void *event_data;
static int signaled,notifications,in_process,cached_error,destroyed;
static struct spa_source *add_event(void *object,spa_source_event_func_t func,void *data)
{ event=func;event_data=data;return &source; }
static int signal_event(void *object,struct spa_source *s)
{ assert(s==&source);++signaled;return 0; }
static void destroy_source(void *object,struct spa_source *s)
{ assert(s==&source);++destroyed; }
static void info(void *data,const struct spa_node_info *info)
{
    assert(!in_process);
    if(info->change_mask&SPA_NODE_CHANGE_MASK_PARAMS)++notifications;
}
static void result(void *data,int seq,int res,uint32_t type,const void *result)
{
    assert(res>=0);if(type!=SPA_RESULT_TYPE_NODE_PARAMS)return;
    const struct spa_result_node_params *p=result;
    struct spa_pod *params;
    assert(spa_pod_parse_object(p->param,SPA_TYPE_OBJECT_Props,NULL,SPA_PROP_params,SPA_POD_Pod(&params))>=0);
    struct spa_pod_parser parser;struct spa_pod_frame frame;
    spa_pod_parser_pod(&parser,params);assert(spa_pod_parser_push_struct(&parser,&frame)>=0);
    const char *key;
    while(spa_pod_parser_get_string(&parser,&key)>=0) {
        if(!strcmp(key,"fxroute.src.error"))assert(spa_pod_parser_get_int(&parser,&cached_error)>=0);
        else {struct spa_pod *value;assert(spa_pod_parser_get(&parser,SPA_POD_Pod(&value),NULL)>=0);}
    }
}
static pid_t worker_child(void)
{
    DIR *tasks=opendir("/proc/self/task");assert(tasks);struct dirent *entry;pid_t child=0;
    while((entry=readdir(tasks))) {
        char path[512];snprintf(path,sizeof(path),"/proc/self/task/%s/children",entry->d_name);
        FILE *file=fopen(path,"r");if(!file)continue;
        int pid;if(fscanf(file,"%d",&pid)==1)child=pid;fclose(file);
        if(child)break;
    }
    closedir(tasks);return child;
}
struct rt_state {
    struct spa_node *node;
    struct spa_io_buffers *io;
    _Atomic int started;
};
static void *rt_drain(void *data)
{
    struct rt_state *state=data;
    for(int n=0;n<80;++n) {
        state->io[0].status=SPA_STATUS_DRAINED;
        state->io[1].status=SPA_STATUS_NEED_DATA;
        assert(spa_node_process(state->node)<0);
        atomic_store(&state->started,1);usleep(1000);
    }
    return NULL;
}
int main(int argc,char **argv)
{
    assert(argc==2||(argc==3&&!strcmp(argv[2],"drain")));int drain=argc==3;
    void *library=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(library);
    spa_handle_factory_enum_func_t enumerate=dlsym(library,SPA_HANDLE_FACTORY_ENUM_FUNC_NAME);
    const struct spa_handle_factory *factory;uint32_t index=0;assert(enumerate(&factory,&index)>0);
    struct spa_loop_utils_methods methods={.version=SPA_VERSION_LOOP_UTILS_METHODS,
        .add_event=add_event,.signal_event=signal_event,.destroy_source=destroy_source};
    struct spa_loop_utils utils={.iface=SPA_INTERFACE_INIT(SPA_TYPE_INTERFACE_LoopUtils,SPA_VERSION_LOOP_UTILS,&methods,NULL)};
    struct spa_support support={SPA_TYPE_INTERFACE_LoopUtils,&utils};
    struct spa_handle *handle=calloc(1,spa_handle_factory_get_size(factory,NULL));
    assert(spa_handle_factory_init(factory,handle,NULL,&support,1)==0);
    struct spa_node *node;assert(spa_handle_get_interface(handle,SPA_TYPE_INTERFACE_Node,(void**)&node)==0);
    struct spa_hook hook;struct spa_node_events events={.version=SPA_VERSION_NODE_EVENTS,.info=info,.result=result};
    assert(spa_node_add_listener(node,&hook,&events,NULL)==0);
    for(int d=0;d<2;++d) {
        char storage[1024];struct spa_pod_builder b=SPA_POD_BUILDER_INIT(storage,sizeof(storage));
        struct spa_audio_info_raw raw={.format=SPA_AUDIO_FORMAT_F32,.rate=d?48000:44100,.channels=2,.position={SPA_AUDIO_CHANNEL_FL,SPA_AUDIO_CHANNEL_FR}};
        assert(spa_node_port_set_param(node,d,0,SPA_PARAM_Format,0,spa_format_audio_raw_build(&b,SPA_PARAM_Format,&raw))==0);
    }
    struct spa_io_position position={.clock={.rate={1,48000},.duration=256,.target_rate={1,48000},.target_duration=256}};
    assert(spa_node_set_io(node,SPA_IO_Position,&position,sizeof(position))==0);
    float pcm[2][8192] __attribute__((aligned(64)))={{0}};
    struct spa_chunk chunks[2]={{.stride=8},{.stride=8}};
    struct spa_data data[2]={{.type=SPA_DATA_MemPtr,.flags=SPA_DATA_FLAG_READWRITE,.fd=-1,.maxsize=sizeof(pcm[0]),.data=pcm[0],.chunk=&chunks[0]},
        {.type=SPA_DATA_MemPtr,.flags=SPA_DATA_FLAG_READWRITE,.fd=-1,.maxsize=sizeof(pcm[1]),.data=pcm[1],.chunk=&chunks[1]}};
    struct spa_buffer buffers[2]={{.n_datas=1,.datas=&data[0]},{.n_datas=1,.datas=&data[1]}};
    struct spa_io_buffers io[2]={SPA_IO_BUFFERS_INIT,SPA_IO_BUFFERS_INIT};
    for(int d=0;d<2;++d) {
        struct spa_buffer *buffer=&buffers[d];
        assert(spa_node_port_use_buffers(node,d,0,0,&buffer,1)==0);
        assert(spa_node_port_set_io(node,d,0,SPA_IO_Buffers,&io[d],sizeof(io[d]))==0);
    }
    struct spa_command command=SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Start);
    assert(spa_node_send_command(node,&command)==0);
    pid_t child=0;for(int n=0;n<1000&&!child;++n){child=worker_child();usleep(1000);}assert(child>0);
    assert(spa_node_enum_params(node,1,SPA_PARAM_Props,0,1,NULL)==0);assert(cached_error==0);
    if(drain) {
        io[0].status=SPA_STATUS_HAVE_DATA;io[0].buffer_id=0;chunks[0].size=256*8;
        io[1].status=SPA_STATUS_NEED_DATA;assert(spa_node_process(node)>=0);
    }
    int before=notifications;assert(kill(child,SIGKILL)==0);
    FILE *diagnostic=tmpfile();assert(diagnostic);int saved_stderr=dup(STDERR_FILENO);assert(saved_stderr>=0);
    assert(dup2(fileno(diagnostic),STDERR_FILENO)>=0);
    int status=0;
    for(int n=0;n<1000&&status>=0;++n) {
        io[0].status=drain?SPA_STATUS_DRAINED:SPA_STATUS_HAVE_DATA;
        io[0].buffer_id=0;chunks[0].offset=0;chunks[0].size=drain?0:256*8;
        io[1].status=SPA_STATUS_NEED_DATA;
        in_process=1;status=spa_node_process(node);in_process=0;usleep(1000);
    }
    assert(status<0);assert(notifications==before);assert(cached_error==0);
    assert(!fflush(stderr));assert(ftell(diagnostic)==0);
    assert(signaled==1&&event);
    struct rt_state state={.node=node,.io=io};pthread_t rt;
    if(drain) {
        assert(pthread_create(&rt,NULL,rt_drain,&state)==0);
        while(!atomic_load(&state.started))usleep(100);
    }
    event(event_data,1);
    if(drain)assert(pthread_join(rt,NULL)==0);
    assert(notifications>before);
    assert(spa_node_enum_params(node,2,SPA_PARAM_Props,0,1,NULL)==0);assert(cached_error!=0);
    assert(!fflush(stderr));rewind(diagnostic);char text[4096]={0};assert(fread(text,1,sizeof(text)-1,diagnostic)>0);
    char code[64];snprintf(code,sizeof(code),"\"error\":%d",cached_error);
    assert(strstr(text,"FXSRC_ERROR_STATS ")&&strstr(text,code));
    assert(strstr(text,"\"channels\":2")&&strstr(text,"\"input_rate\":44100")&&strstr(text,"\"output_rate\":48000"));
    assert(strstr(text,"\"accepted\":")&&strstr(text,"\"produced\":")&&strstr(text,"\"max_input\":")&&strstr(text,"\"lead\":"));
    /* Failure before the reserve ends: no real output, so first_real_ns stays 0. */
    assert(strstr(text,"\"reserve\":")&&strstr(text,"\"first_output_ns\":")&&strstr(text,"\"first_real_ns\":0}"));
    assert(!strstr(strstr(text,"FXSRC_ERROR_STATS ")+1,"FXSRC_ERROR_STATS "));
    spa_hook_remove(&hook);spa_handle_clear(handle);assert(destroyed==1);free(handle);dlclose(library);
    assert(!fflush(stderr));assert(dup2(saved_stderr,STDERR_FILENO)>=0);close(saved_stderr);fclose(diagnostic);
    return 0;
}
