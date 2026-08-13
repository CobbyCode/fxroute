#include "dsp.h"

#include <pipewire/pipewire.h>
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

struct engine {
    struct pw_main_loop *loop;
    struct pw_filter *filter;
    fxdsp *dsp;
    void *input[FXDSP_MAX_CHANNELS];
    void *output[FXDSP_MAX_CHANNELS];
    int control_fd;
    const char *control_path;
    pthread_t control_thread;
    _Atomic int control_stop;
};

static void reply_peaks(struct engine *engine, const struct sockaddr_un *client, socklen_t client_size) {
    float peaks[FXDSP_MAX_CHANNELS];
    char reply[1024];
    unsigned outputs=fxdsp_peaks(engine->dsp,peaks,FXDSP_MAX_CHANNELS);
    size_t used=(size_t)snprintf(reply,sizeof reply,"{\"peaks\":[");
    for(unsigned i=0;i<outputs&&used<sizeof reply;i++)
        used+=(size_t)snprintf(reply+used,sizeof reply-used,"%s%.9g",i?",":"",peaks[i]);
    if(used<sizeof reply)used+=(size_t)snprintf(reply+used,sizeof reply-used,"]}\n");
    if(used>sizeof reply)used=sizeof reply;
    (void)sendto(engine->control_fd,reply,used,0,(const struct sockaddr *)client,client_size);
}

static void handle_control(struct engine *engine, char *command, const struct sockaddr_un *client, socklen_t client_size) {
    static const char mute_command[]="mute";
    static const char peaks_reset_command[]="peaks reset";
    static const char peaks_get_command[]="peaks get";
    long mask;
    int value;
    char extra;
    if(sscanf(command,"mute %li %d %c",&mask,&value,&extra)==2&&!strncmp(command,mute_command,sizeof mute_command-1)&&(value==0||value==1)&&mask>=0&&(unsigned long)mask<=UINT32_MAX) {
        fxdsp_set_mute(engine->dsp,(uint32_t)mask,value);
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    }
    else if(!strcmp(command,peaks_reset_command)) {
        fxdsp_reset_peaks(engine->dsp);
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    }
    else if(!strcmp(command,peaks_get_command))reply_peaks(engine,client,client_size);
}

static void *control_main(void *data) {
    struct engine *engine=data;
    struct pollfd poll_fd={.fd=engine->control_fd,.events=POLLIN};
    while(!atomic_load_explicit(&engine->control_stop,memory_order_relaxed)) {
        if(poll(&poll_fd,1,100)<=0)continue;
        struct sockaddr_un client;
        socklen_t client_size=sizeof client;
        char command[256];
        ssize_t size=recvfrom(engine->control_fd,command,sizeof command-1,0,(struct sockaddr *)&client,&client_size);
        if(size<0){if(errno==EINTR)continue;break;}
        command[size]='\0';
        while(size>0&&(command[size-1]=='\n'||command[size-1]=='\r'))command[--size]='\0';
        handle_control(engine,command,&client,client_size);
    }
    return NULL;
}

static int start_control(struct engine *engine,const char *path) {
    struct sockaddr_un address={.sun_family=AF_UNIX};
    if(strlen(path)>=sizeof address.sun_path){fprintf(stderr,"control socket path too long\n");return -1;}
    memcpy(address.sun_path,path,strlen(path)+1);
    engine->control_fd=socket(AF_UNIX,SOCK_DGRAM,0);
    if(engine->control_fd<0){perror("control socket");return -1;}
    unlink(path);
    if(bind(engine->control_fd,(struct sockaddr *)&address,sizeof address)<0){perror("bind control socket");close(engine->control_fd);engine->control_fd=-1;return -1;}
    engine->control_path=path;
    if(pthread_create(&engine->control_thread,NULL,control_main,engine)){fprintf(stderr,"cannot start control thread\n");close(engine->control_fd);unlink(path);engine->control_fd=-1;return -1;}
    return 0;
}

static void stop_control(struct engine *engine) {
    if(engine->control_fd<0)return;
    atomic_store_explicit(&engine->control_stop,1,memory_order_relaxed);
    pthread_join(engine->control_thread,NULL);
    close(engine->control_fd);
    unlink(engine->control_path);
}

static void on_process(void *data, struct spa_io_position *position) {
    struct engine *engine = data;
    uint32_t frames = position && position->clock.duration ? position->clock.duration : 1024;
    const float *input[FXDSP_MAX_CHANNELS];
    float *output[FXDSP_MAX_CHANNELS];
    int complete = 1;
    for (unsigned i = 0; i < fxdsp_inputs(engine->dsp); i++) {
        input[i] = pw_filter_get_dsp_buffer(engine->input[i], frames);
        if (!input[i]) complete = 0;
    }
    for (unsigned i = 0; i < fxdsp_outputs(engine->dsp); i++) {
        output[i] = pw_filter_get_dsp_buffer(engine->output[i], frames);
        if (!output[i]) complete = 0;
    }
    if (!complete) {
        for (unsigned i = 0; i < fxdsp_outputs(engine->dsp); i++)
            if (output[i]) memset(output[i], 0, frames * sizeof *output[i]);
        return;
    }
    fxdsp_process(engine->dsp, input, output, frames);
}

static const struct pw_filter_events filter_events = {
    PW_VERSION_FILTER_EVENTS,
    .process = on_process,
};

static struct engine *signal_engine;
static void stop_engine(int signal_number) {
    (void)signal_number;
    if (signal_engine) pw_main_loop_quit(signal_engine->loop);
}

int main(int argc, char **argv) {
    char error[256];
    struct engine engine = {.control_fd=-1};
    if (argc != 2 && argc != 3) {
        fprintf(stderr, "usage: %s CONFIG [CONTROL_SOCKET]\n", argv[0]);
        return 2;
    }
    engine.dsp = fxdsp_load(argv[1], error, sizeof error);
    if (!engine.dsp) {
        fprintf(stderr, "%s\n", error);
        return 1;
    }
    pw_init(&argc, &argv);
    engine.loop = pw_main_loop_new(NULL);
    if (!engine.loop) {
        fprintf(stderr, "cannot create PipeWire main loop\n");
        fxdsp_free(engine.dsp); pw_deinit();
        return 1;
    }
    engine.filter = pw_filter_new_simple(
        pw_main_loop_get_loop(engine.loop), "fxroute-dsp",
        pw_properties_new(PW_KEY_MEDIA_TYPE, "Audio", PW_KEY_MEDIA_CATEGORY, "Filter",
                          PW_KEY_MEDIA_ROLE, "DSP", PW_KEY_NODE_NAME, "fxroute_dsp", NULL),
        &filter_events, &engine);
    if (!engine.filter) {
        fprintf(stderr, "cannot create PipeWire filter\n");
        pw_main_loop_destroy(engine.loop); fxdsp_free(engine.dsp); pw_deinit();
        return 1;
    }
    for (unsigned i = 0; i < fxdsp_inputs(engine.dsp); i++) {
        char name[32]; snprintf(name, sizeof name, "input_%u", i + 1);
        engine.input[i] = pw_filter_add_port(engine.filter, PW_DIRECTION_INPUT,
            PW_FILTER_PORT_FLAG_MAP_BUFFERS, 0,
            pw_properties_new(PW_KEY_FORMAT_DSP, "32 bit float mono audio", PW_KEY_PORT_NAME, name, NULL), NULL, 0);
    }
    for (unsigned i = 0; i < fxdsp_outputs(engine.dsp); i++) {
        char name[32]; snprintf(name, sizeof name, "output_%u", i + 1);
        engine.output[i] = pw_filter_add_port(engine.filter, PW_DIRECTION_OUTPUT,
            PW_FILTER_PORT_FLAG_MAP_BUFFERS, 0,
            pw_properties_new(PW_KEY_FORMAT_DSP, "32 bit float mono audio", PW_KEY_PORT_NAME, name, NULL), NULL, 0);
    }
    if (pw_filter_connect(engine.filter, PW_FILTER_FLAG_RT_PROCESS, NULL, 0) < 0) {
        fprintf(stderr, "cannot connect PipeWire filter\n");
        pw_filter_destroy(engine.filter); pw_main_loop_destroy(engine.loop);
        fxdsp_free(engine.dsp); pw_deinit();
        return 1;
    }
    if(argc==3&&start_control(&engine,argv[2])) {
        pw_filter_destroy(engine.filter); pw_main_loop_destroy(engine.loop);
        fxdsp_free(engine.dsp); pw_deinit();
        return 1;
    }
    signal_engine = &engine;
    signal(SIGINT, stop_engine); signal(SIGTERM, stop_engine);
    pw_main_loop_run(engine.loop);
    pw_filter_destroy(engine.filter); pw_main_loop_destroy(engine.loop); stop_control(&engine);
    fxdsp_free(engine.dsp); pw_deinit();
    return 0;
}
