/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include <assert.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <spa/node/node.h>
#include <spa/node/io.h>
#include <spa/node/command.h>
#include <spa/param/audio/format-utils.h>
#include <spa/param/audio/dsp-utils.h>
#include <spa/param/port-config.h>
#include <spa/support/plugin.h>

int main(int argc,char **argv)
{
    assert(argc==3||argc==5);void *lib=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(lib);
    FILE *source=argc==5?fopen(argv[3],"rb"):NULL,*target=argc==5?fopen(argv[4],"wb"):NULL;
    if(argc==5)assert(source&&target);
    spa_handle_factory_enum_func_t enumerate=dlsym(lib,SPA_HANDLE_FACTORY_ENUM_FUNC_NAME);
    const struct spa_handle_factory *factory;uint32_t index=0;assert(enumerate(&factory,&index)>0);
    struct spa_dict_item items[]={{"convert.direction","output"},{"clock.quantum-limit","8192"}};
    struct spa_dict properties=SPA_DICT_INIT(items,2);
    struct spa_handle *handle=calloc(1,spa_handle_factory_get_size(factory,&properties));
    assert(spa_handle_factory_init(factory,handle,&properties,NULL,0)==0);
    struct spa_node *node;assert(spa_handle_get_interface(handle,SPA_TYPE_INTERFACE_Node,(void**)&node)==0);
    char storage[2048];struct spa_pod_builder builder=SPA_POD_BUILDER_INIT(storage,sizeof(storage));
    struct spa_audio_info_raw raw={.format=SPA_AUDIO_FORMAT_F32,.rate=48000,.channels=18};
    for(int c=0;c<18;++c)raw.position[c]=SPA_AUDIO_CHANNEL_AUX0+c;
    const struct spa_pod *format=spa_format_audio_raw_build(&builder,SPA_PARAM_Format,&raw);
    assert(spa_node_port_set_param(node,SPA_DIRECTION_OUTPUT,0,SPA_PARAM_Format,0,format)==0);
    const struct spa_pod *config=spa_pod_builder_add_object(&builder,SPA_TYPE_OBJECT_ParamPortConfig,SPA_PARAM_PortConfig,
        SPA_PARAM_PORT_CONFIG_direction,SPA_POD_Id(SPA_DIRECTION_INPUT),
        SPA_PARAM_PORT_CONFIG_mode,SPA_POD_Id(SPA_PARAM_PORT_CONFIG_MODE_dsp),
        SPA_PARAM_PORT_CONFIG_format,SPA_POD_Pod(format));
    assert(spa_node_set_param(node,SPA_PARAM_PortConfig,0,config)==0);
    struct spa_audio_info_dsp dsp={.format=SPA_AUDIO_FORMAT_DSP_F32};
    spa_pod_builder_init(&builder,storage,sizeof(storage));format=spa_format_audio_dsp_build(&builder,SPA_PARAM_Format,&dsp);
    for(int c=0;c<18;++c)assert(spa_node_port_set_param(node,SPA_DIRECTION_INPUT,c,SPA_PARAM_Format,0,format)==0);
    struct spa_io_position position={.clock={.rate={1,96000},.duration=256,.target_rate={1,96000},.target_duration=256}};
    assert(spa_node_set_io(node,SPA_IO_Position,&position,sizeof(position))==0);
    struct spa_io_rate_match match={.flags=SPA_IO_RATE_MATCH_FLAG_ACTIVE,.rate=1.0};
    assert(spa_node_port_set_io(node,SPA_DIRECTION_INPUT,0,SPA_IO_RateMatch,&match,sizeof(match))==0);
    static float pcm[19][8192*18] __attribute__((aligned(64)));
    struct spa_chunk chunks[19]={0};struct spa_data data[19]={0};
    struct spa_buffer buffers[19]={0};struct spa_io_buffers io[19];
    for(int p=0;p<19;++p) {
        unsigned dir=p==18?SPA_DIRECTION_OUTPUT:SPA_DIRECTION_INPUT,port=p==18?0:p;
        chunks[p].stride=p==18?72:4;
        data[p]=(struct spa_data){.type=SPA_DATA_MemPtr,.flags=SPA_DATA_FLAG_READWRITE,.fd=-1,.maxsize=sizeof(pcm[p]),.data=pcm[p],.chunk=&chunks[p]};
        buffers[p]=(struct spa_buffer){.n_datas=1,.datas=&data[p]};io[p]=(struct spa_io_buffers)SPA_IO_BUFFERS_INIT;
        struct spa_buffer *buffer=&buffers[p];
        assert(spa_node_port_use_buffers(node,dir,port,0,&buffer,1)==0);
        assert(spa_node_port_set_io(node,dir,port,SPA_IO_Buffers,&io[p],sizeof(io[p]))==0);
    }
    struct spa_command command=SPA_NODE_COMMAND_INIT(SPA_NODE_COMMAND_Start);assert(spa_node_send_command(node,&command)==0);
    unsigned submitted=0,made=0,drained=0;
    for(int tick=0;tick<1500&&!drained;++tick) {
        unsigned rate=48000;
        if((!strcmp(argv[2],"2-1")&&tick>=100)||(!strcmp(argv[2],"2-1-2")&&tick>=100&&tick<150)||(!strcmp(argv[2],"1-2")&&tick<100))rate=96000;
        position.clock.rate.denom=position.clock.target_rate.denom=rate;
        if(io[0].status!=SPA_STATUS_HAVE_DATA) {
            for(int c=0;c<18;++c) {
                if(submitted>=96000)io[c].status=SPA_STATUS_DRAINED;
                else {io[c].status=SPA_STATUS_HAVE_DATA;io[c].buffer_id=0;chunks[c].offset=0;chunks[c].size=256*4;}
            }
            if(submitted<96000) {
                if(source) {
                    float interleaved[256*18];assert(fread(interleaved,sizeof(float)*18,256,source)==256);
                    for(int c=0;c<18;++c)for(int i=0;i<256;++i)pcm[c][i]=interleaved[i*18+c];
                }
                submitted+=256;
            }
        }
        io[18].status=SPA_STATUS_NEED_DATA;
        int status=spa_node_process(node);
        if(status<0){fprintf(stderr,"Clock handoff failed: process=%d submitted=%u made=%u\n",status,submitted,made);return 3;}
        if(io[18].status&SPA_STATUS_HAVE_DATA) {
            made+=chunks[18].size/72;
            if(target)assert(fwrite(pcm[18],1,chunks[18].size,target)==chunks[18].size);
        }
        if(status&SPA_STATUS_DRAINED)drained=1;
        usleep(256*1000000/rate);
    }
    fprintf(stderr,"Clock handoff: drained=%u submitted=%u made=%u\n",drained,submitted,made);
    void (*complete)(void)=dlsym(lib,"fx_verify_complete");if(complete)complete();
    spa_handle_clear(handle);free(handle);dlclose(lib);
    if(source)fclose(source);if(target)fclose(target);
    assert(drained&&submitted==96000&&made>=84000);return 0;
}
