#include "dsp.h"

#include <pipewire/pipewire.h>
#include <errno.h>
#include <math.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
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
    _Atomic(fxdsp *) dsp;
    _Atomic unsigned processing;
    void *input[FXDSP_MAX_CHANNELS];
    void *output[FXDSP_MAX_CHANNELS];
    void *post_effect[2];
    int control_fd;
    const char *control_path;
    pthread_t control_thread;
    _Atomic int control_stop;
};

static void reply_peaks(struct engine *engine, const struct sockaddr_un *client, socklen_t client_size) {
    float peaks[FXDSP_MAX_CHANNELS];
    char reply[1024];
    unsigned outputs=fxdsp_peaks(atomic_load_explicit(&engine->dsp,memory_order_acquire),peaks,FXDSP_MAX_CHANNELS);
    size_t used=(size_t)snprintf(reply,sizeof reply,"{\"peaks\":[");
    for(unsigned i=0;i<outputs&&used<sizeof reply;i++)
        used+=(size_t)snprintf(reply+used,sizeof reply-used,"%s%.9g",i?",":"",peaks[i]);
    if(used<sizeof reply)used+=(size_t)snprintf(reply+used,sizeof reply-used,"]}\n");
    if(used>sizeof reply)used=sizeof reply;
    (void)sendto(engine->control_fd,reply,used,0,(const struct sockaddr *)client,client_size);
}

static int fxdsp_swap_config(struct engine *engine, const char *path, float gain_db) {
    char error[256];
    fxdsp *candidate = fxdsp_load(path, error, sizeof error);
    fxdsp *previous;
    if (!candidate || !isfinite(gain_db) || gain_db < -80.0f || gain_db > 0.0f) {
        fxdsp_free(candidate);
        return 0;
    }
    previous = atomic_load_explicit(&engine->dsp, memory_order_acquire);
    if (!fxdsp_compatible(previous, candidate)) {
        fxdsp_free(candidate);
        return 0;
    }
    fxdsp_set_output_gain_db(candidate, gain_db);
    previous = atomic_exchange_explicit(&engine->dsp, candidate, memory_order_acq_rel);
    while (atomic_load_explicit(&engine->processing, memory_order_acquire) != 0)
        sched_yield();
    fxdsp_free(previous);
    return 1;
}

static void handle_control(struct engine *engine, char *command, const struct sockaddr_un *client, socklen_t client_size) {
    static const char mute_command[]="mute";
    static const char peaks_reset_command[]="peaks reset";
    static const char peaks_get_command[]="peaks get";
    static const char effects_bypass_command[]="effects bypass";
    static const char gain_db_command[]="gain db";
    long mask;
    int value;
    char extra;
    char path[1024];
    char stage_id[256], symbol[128], type[32], polarity[16];
    unsigned first, second;
    float live_value, live_value2, live_value3;
    if(sscanf(command,"mute %li %d %c",&mask,&value,&extra)==2&&!strncmp(command,mute_command,sizeof mute_command-1)&&(value==0||value==1)&&mask>=0&&(unsigned long)mask<=UINT32_MAX) {
        fxdsp_set_mute(atomic_load_explicit(&engine->dsp,memory_order_acquire),(uint32_t)mask,value);
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    }
    else if(!strcmp(command,peaks_reset_command)) {
        fxdsp_reset_peaks(atomic_load_explicit(&engine->dsp,memory_order_acquire));
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    }
    else if(!strcmp(command,peaks_get_command))reply_peaks(engine,client,client_size);
    else if(sscanf(command,"effects bypass %d %c",&value,&extra)==1&&!strncmp(command,effects_bypass_command,sizeof effects_bypass_command-1)&&(value==0||value==1)) {
        fxdsp_set_effect_bypass(atomic_load_explicit(&engine->dsp,memory_order_acquire),value);
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    }
    else if(!strcmp(command,"effects bypass get")) {
        char reply[16]; int size=snprintf(reply,sizeof reply,"%d\n",fxdsp_effect_bypass(atomic_load_explicit(&engine->dsp,memory_order_acquire)));
        (void)sendto(engine->control_fd,reply,(size_t)size,0,(const struct sockaddr *)client,client_size);
    }
    else if(!strcmp(command,"live begin") && fxdsp_live_begin(atomic_load_explicit(&engine->dsp,memory_order_acquire)))
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    else if(!strcmp(command,"live commit") && fxdsp_live_commit(atomic_load_explicit(&engine->dsp,memory_order_acquire)))
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    else if(sscanf(command,"live control %255s %127s %f %c",stage_id,symbol,&live_value,&extra)==3 &&
            fxdsp_live_control(atomic_load_explicit(&engine->dsp,memory_order_acquire),stage_id,symbol,live_value))
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    else if(sscanf(command,"live param %255s %127s %f %c",stage_id,symbol,&live_value,&extra)==3 &&
            fxdsp_live_param(atomic_load_explicit(&engine->dsp,memory_order_acquire),stage_id,symbol,live_value))
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    else if(sscanf(command,"live matrix %u %u %f %c",&first,&second,&live_value,&extra)==3 &&
            fxdsp_live_matrix(atomic_load_explicit(&engine->dsp,memory_order_acquire),first,second,live_value))
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    else if(sscanf(command,"live peq %u %u %31s %f %f %f %c",&first,&second,type,&live_value,&live_value2,&live_value3,&extra)==6 &&
            fxdsp_live_peq(atomic_load_explicit(&engine->dsp,memory_order_acquire),first,second,type,live_value,live_value2,live_value3))
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    else if(sscanf(command,"live output %u %f %f %15s %c",&first,&live_value,&live_value2,polarity,&extra)==4 &&
            fxdsp_live_output(atomic_load_explicit(&engine->dsp,memory_order_acquire),first,live_value,live_value2,!strcmp(polarity,"invert")))
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    else { float gain_db;
        if(sscanf(command,"gain db %f %c",&gain_db,&extra)==1&&!strncmp(command,gain_db_command,sizeof gain_db_command-1)&&isfinite(gain_db)&&gain_db>=-80.0f&&gain_db<=0.0f) {
            fxdsp_set_output_gain_db(atomic_load_explicit(&engine->dsp,memory_order_acquire),gain_db);
            (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
        } else if(!strcmp(command,"gain db get")) {
            char reply[32]; int size=snprintf(reply,sizeof reply,"%.9g\n",fxdsp_output_gain_db(atomic_load_explicit(&engine->dsp,memory_order_acquire)));
            (void)sendto(engine->control_fd,reply,(size_t)size,0,(const struct sockaddr *)client,client_size);
        } else if (sscanf(command,"swap config %1023s %f %c",path,&gain_db,&extra)==2 &&
                   fxdsp_swap_config(engine, path, gain_db)) {
            (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
        } else (void)sendto(engine->control_fd,"error invalid command\n",22,0,(const struct sockaddr *)client,client_size);
    }
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
    atomic_fetch_add_explicit(&engine->processing, 1, memory_order_acquire);
    fxdsp *dsp = atomic_load_explicit(&engine->dsp, memory_order_acquire);
    uint32_t frames = position && position->clock.duration ? position->clock.duration : 1024;
    const float *input[FXDSP_MAX_CHANNELS];
    float *output[FXDSP_MAX_CHANNELS];
    float *post_effect[2];
    int complete = 1;
    for (unsigned i = 0; i < fxdsp_inputs(dsp); i++) {
        input[i] = pw_filter_get_dsp_buffer(engine->input[i], frames);
        if (!input[i]) complete = 0;
    }
    for (unsigned i = 0; i < fxdsp_outputs(dsp); i++) {
        output[i] = pw_filter_get_dsp_buffer(engine->output[i], frames);
        if (!output[i]) complete = 0;
    }
    for (unsigned i = 0; i < 2; i++) {
        post_effect[i] = pw_filter_get_dsp_buffer(engine->post_effect[i], frames);
    }
    if (!complete) {
        for (unsigned i = 0; i < fxdsp_outputs(dsp); i++)
            if (output[i]) memset(output[i], 0, frames * sizeof *output[i]);
        for (unsigned i = 0; i < 2; i++)
            if (post_effect[i]) memset(post_effect[i], 0, frames * sizeof *post_effect[i]);
        atomic_fetch_sub_explicit(&engine->processing, 1, memory_order_release);
        return;
    }
    fxdsp_process_tapped(dsp, input, output,
                         post_effect[0] && post_effect[1] ? post_effect : NULL, frames);
    atomic_fetch_sub_explicit(&engine->processing, 1, memory_order_release);
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
    fxdsp *initial_dsp = fxdsp_load(argv[1], error, sizeof error);
    if (!initial_dsp) {
        fprintf(stderr, "%s\n", error);
        return 1;
    }
    pw_init(&argc, &argv);
    engine.loop = pw_main_loop_new(NULL);
    if (!engine.loop) {
        fprintf(stderr, "cannot create PipeWire main loop\n");
        fxdsp_free(initial_dsp); pw_deinit();
        return 1;
    }
    engine.filter = pw_filter_new_simple(
        pw_main_loop_get_loop(engine.loop), "fxroute-dsp",
        pw_properties_new(PW_KEY_MEDIA_TYPE, "Audio", PW_KEY_MEDIA_CATEGORY, "Filter",
                          PW_KEY_MEDIA_ROLE, "DSP", PW_KEY_NODE_NAME, "fxroute_dsp", NULL),
        &filter_events, &engine);
    if (!engine.filter) {
        fprintf(stderr, "cannot create PipeWire filter\n");
        pw_main_loop_destroy(engine.loop); fxdsp_free(initial_dsp); pw_deinit();
        return 1;
    }
    atomic_init(&engine.dsp, initial_dsp);
    atomic_init(&engine.processing, 0);
    for (unsigned i = 0; i < fxdsp_inputs(initial_dsp); i++) {
        char name[32]; snprintf(name, sizeof name, "input_%u", i + 1);
        engine.input[i] = pw_filter_add_port(engine.filter, PW_DIRECTION_INPUT,
            PW_FILTER_PORT_FLAG_MAP_BUFFERS, 0,
            pw_properties_new(PW_KEY_FORMAT_DSP, "32 bit float mono audio", PW_KEY_PORT_NAME, name, NULL), NULL, 0);
    }
    for (unsigned i = 0; i < fxdsp_outputs(initial_dsp); i++) {
        char name[32]; snprintf(name, sizeof name, "output_%u", i + 1);
        engine.output[i] = pw_filter_add_port(engine.filter, PW_DIRECTION_OUTPUT,
            PW_FILTER_PORT_FLAG_MAP_BUFFERS, 0,
            pw_properties_new(PW_KEY_FORMAT_DSP, "32 bit float mono audio", PW_KEY_PORT_NAME, name, NULL), NULL, 0);
    }
    engine.post_effect[0] = pw_filter_add_port(engine.filter, PW_DIRECTION_OUTPUT,
        PW_FILTER_PORT_FLAG_MAP_BUFFERS, 0,
        pw_properties_new(PW_KEY_FORMAT_DSP, "32 bit float mono audio", PW_KEY_PORT_NAME, "post_effect_FL", NULL), NULL, 0);
    engine.post_effect[1] = pw_filter_add_port(engine.filter, PW_DIRECTION_OUTPUT,
        PW_FILTER_PORT_FLAG_MAP_BUFFERS, 0,
        pw_properties_new(PW_KEY_FORMAT_DSP, "32 bit float mono audio", PW_KEY_PORT_NAME, "post_effect_FR", NULL), NULL, 0);
    if (pw_filter_connect(engine.filter, PW_FILTER_FLAG_RT_PROCESS, NULL, 0) < 0) {
        fprintf(stderr, "cannot connect PipeWire filter\n");
        pw_filter_destroy(engine.filter); pw_main_loop_destroy(engine.loop);
        fxdsp_free(atomic_load(&engine.dsp)); pw_deinit();
        return 1;
    }
    if(argc==3&&start_control(&engine,argv[2])) {
        pw_filter_destroy(engine.filter); pw_main_loop_destroy(engine.loop);
        fxdsp_free(atomic_load(&engine.dsp)); pw_deinit();
        return 1;
    }
    signal_engine = &engine;
    signal(SIGINT, stop_engine); signal(SIGTERM, stop_engine);
    pw_main_loop_run(engine.loop);
    pw_filter_destroy(engine.filter); pw_main_loop_destroy(engine.loop); stop_control(&engine);
    fxdsp_free(atomic_load(&engine.dsp)); pw_deinit();
    return 0;
}
