/* SPDX-License-Identifier: AGPL-3.0-only */
#include <assert.h>
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
#include <spa/node/node.h>
#include <spa/node/command.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/props.h>
#include <spa/support/plugin.h>

static int seen;
static int peaks;
static int seen_latency;
static void result(void *data, int seq, int res, uint32_t type, const void *result)
{
    (void)data;(void)seq;assert(res>=0);
    if(type!=SPA_RESULT_TYPE_NODE_PARAMS)return;
    const struct spa_result_node_params *p=result;
    assert(p->id==SPA_PARAM_Props);
    struct spa_pod *params;
    assert(spa_pod_parse_object(p->param,SPA_TYPE_OBJECT_Props,NULL,SPA_PROP_params,SPA_POD_Pod(&params))>=0);
    struct spa_pod_parser parser;struct spa_pod_frame frame;
    spa_pod_parser_pod(&parser,params);assert(spa_pod_parser_push_struct(&parser,&frame)>=0);
    const char *key,*mode;
    while(spa_pod_parser_get_string(&parser,&key)>=0) {
        if(!strcmp(key,"fxroute.src.mode")) {
            assert(spa_pod_parser_get_string(&parser,&mode)>=0);
            assert(!peaks||!strcmp(mode,"peaks"));seen=1;
        } else if(!strcmp(key,"fxroute.src.latency-ns")) {
            int64_t latency;assert(spa_pod_parser_get_long(&parser,&latency)>=0);
            assert(latency==(peaks?0:750000000));seen_latency=1;
        } else {
            struct spa_pod *value;assert(spa_pod_parser_get(&parser,SPA_POD_Pod(&value),NULL)>=0);
        }
    }
}
int main(int argc,char **argv)
{
    assert(argc==2||argc==3);peaks=argc==2;
    unsigned channels=peaks?2:atoi(argv[2]);assert(channels<=32);
    void *lib=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(lib);
    spa_handle_factory_enum_func_t enumerate=dlsym(lib,SPA_HANDLE_FACTORY_ENUM_FUNC_NAME);
    const struct spa_handle_factory *factory;uint32_t index=0;assert(enumerate(&factory,&index)>0);
    struct spa_dict_item items[]={{"resample.peaks",peaks?"true":"false"}};
    struct spa_dict info=SPA_DICT_INIT(items,1);
    struct spa_handle *handle=calloc(1,spa_handle_factory_get_size(factory,&info));
    assert(spa_handle_factory_init(factory,handle,&info,NULL,0)==0);
    struct spa_node *node;assert(spa_handle_get_interface(handle,SPA_TYPE_INTERFACE_Node,(void**)&node)==0);
    struct spa_hook hook;struct spa_node_events events={.version=SPA_VERSION_NODE_EVENTS,.result=result};
    assert(spa_node_add_listener(node,&hook,&events,NULL)==0);
    for(int d=0;d<2;++d) {
        char storage[1024];struct spa_pod_builder b=SPA_POD_BUILDER_INIT(storage,sizeof(storage));
        struct spa_audio_info_raw raw={.format=SPA_AUDIO_FORMAT_F32,.rate=d?48000:44100,.channels=channels};
        for(unsigned c=0;c<channels;++c)raw.position[c]=SPA_AUDIO_CHANNEL_AUX0+c;
        assert(spa_node_port_set_param(node,d,0,SPA_PARAM_Format,0,spa_format_audio_raw_build(&b,SPA_PARAM_Format,&raw))==0);
    }
    struct spa_command cmd=SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Start);
    assert(spa_node_send_command(node,&cmd)==0);
    assert(spa_node_enum_params(node,1,SPA_PARAM_Props,0,1,NULL)==0);assert(seen&&seen_latency);
    spa_hook_remove(&hook);spa_handle_clear(handle);free(handle);dlclose(lib);
    return 0;
}
