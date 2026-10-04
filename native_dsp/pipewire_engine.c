#include "dsp.h"

#include <pipewire/pipewire.h>
#include <pipewire/thread.h>
#include <inttypes.h>
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
    struct pw_context *context;
    struct pw_core *core;
    struct spa_thread_utils *thread_utils;
    struct spa_hook filter_listener;
    struct pw_filter *filter;
    /* One helper pool for the whole process.  Owned by the engine, borrowed by
     * every graph: helpers are a machine property, not a graph property. */
    fx_workers *workers;
    unsigned worker_limit;
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

static void *create_worker(void *(*function)(void *), void *argument, void *context) {
    struct spa_dict_item items[] = {{SPA_KEY_THREAD_NAME, "fxdsp-worker"}};
    return spa_thread_utils_create(context, &SPA_DICT_INIT_ARRAY(items), function, argument);
}
static void join_worker(void *thread, void *context) {
    (void)spa_thread_utils_join(context, thread, NULL);
}
static int acquire_worker_rt(void *thread, int priority, void *context) {
    return spa_thread_utils_acquire_rt(context, thread, priority);
}
/* Pool capacity is fixed before the filter activates and never changes: it is
 * derived from the channel layout, the CPU affinity and FXROUTE_DSP_WORKERS,
 * read once here instead of on every graph load.  Graph swaps then reuse the
 * same verified RT helpers. */
static fx_workers *start_workers(struct engine *engine, const fxdsp *dsp) {
    fx_worker_policy policy = {.count=FX_WORKER_LIMIT, .require_rt=1,
                              .priority=-1, .create_thread=create_worker, .join_thread=join_worker,
                              .acquire_rt=engine->thread_utils ? acquire_worker_rt : NULL,
                              .context=engine->thread_utils};
    engine->worker_limit = fxdsp_engine_worker_limit(dsp, &policy);
    policy.count = engine->worker_limit;
    return fx_workers_create(&policy);
}
static const char *worker_mode(const struct engine *engine, unsigned jobs) {
    if (!engine->worker_limit) return "disabled";
    if (!engine->workers) return "degraded";
    return jobs > 1 ? "pool" : "serial";
}
static void log_worker_status(const struct engine *engine, unsigned jobs) {
    int priority, scheduler = fx_workers_scheduler(engine->workers, &priority);
    fprintf(stderr, "DSP worker capacity: %u/%u, mode: %s, scheduler: %s, priority: %d\n",
            fx_workers_count(engine->workers), engine->worker_limit, worker_mode(engine, jobs),
            scheduler == SCHED_RR ? "RR" : scheduler == SCHED_FIFO ? "FIFO" : "OTHER", priority);
}
/* Helpers a graph actually engages: the pool is sized for the widest possible
 * dispatch, a serial graph uses none of it. */
static unsigned engaged_helpers(const struct engine *engine, unsigned jobs) {
    unsigned capacity = fx_workers_count(engine->workers);
    if (!jobs || !capacity) return 0;
    return capacity < jobs - 1 ? capacity : jobs - 1;
}
/* The candidate is fully prepared before it is published: the borrowed pool,
 * the per-graph parallel-output selection and the timing mode are all fixed
 * here while nothing processes the candidate.  Kept before the exchange
 * because the callback reads plain fields this writes. */
static unsigned prepare_graph(struct engine *engine, fxdsp *dsp) {
    fxdsp_borrow_workers(dsp, engine->workers);
    const char *timing = getenv("FXROUTE_DSP_TIMING");
    fxdsp_enable_timing(dsp, timing && !strcmp(timing, "1"));
    return fxdsp_worker_demand(dsp);
}
static void reply_timing(struct engine *engine, const struct sockaddr_un *client, socklen_t client_size) {
    fxdsp *dsp = atomic_load_explicit(&engine->dsp, memory_order_acquire);
    char reply[1024]; int priority;
    int scheduler = fx_workers_scheduler(engine->workers, &priority);
    unsigned jobs = dsp ? fxdsp_worker_demand(dsp) : 0;
    size_t used = (size_t)snprintf(reply, sizeof reply,
                                 "{\"workers\":%u,\"worker_limit\":%u,\"worker_jobs\":%u,\"worker_used\":%u,"
                                 "\"worker_mode\":\"%s\",\"scheduler\":%d,\"priority\":%d",
                                 fx_workers_count(engine->workers), engine->worker_limit, jobs,
                                 engaged_helpers(engine, jobs), worker_mode(engine, jobs), scheduler, priority);
    for (unsigned i = 0; i < FXDSP_TIMING_COUNT; i++) {
        fx_timing_summary t = fxdsp_read_timing(dsp, i);
        used += (size_t)snprintf(reply + used, sizeof reply - used,
                                ",\"%s\":{\"samples\":%" PRIu64 ",\"p99_upper_ms\":%.6f,\"max_ms\":%.6f}",
                                 fxdsp_timing_name(i), t.samples, t.p99_ns * 1e-6, t.max_ns * 1e-6);
    }
    used += (size_t)snprintf(reply + used, sizeof reply - used, "}\n");
    (void)sendto(engine->control_fd, reply, used, 0, (const struct sockaddr *)client, client_size);
}

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
    unsigned demand = prepare_graph(engine, candidate);
    if (demand != fxdsp_worker_demand(previous)) {
        /* Same pool, different spread: report it once per change instead of
         * creating, admitting and joining helpers again. */
        fprintf(stderr, "DSP graph swap: %u jobs, %u/%u helpers\n", demand,
                engaged_helpers(engine, demand), fx_workers_count(engine->workers));
    }
    /* Epoch reclamation: the processing counter and the dsp pointer must be
     * seq_cst.  A reader (on_process) increments the counter, then loads the
     * pointer; the writer swaps the pointer, then drains the counter before
     * freeing the previous graph.  With weaker ordering a reader that already
     * loaded the old pointer is not guaranteed to have published its in-flight
     * flag to the writer, so the writer could free the graph under it on
     * weakly ordered cores (use-after-free). */
    previous = atomic_exchange_explicit(&engine->dsp, candidate, memory_order_seq_cst);
    while (atomic_load_explicit(&engine->processing, memory_order_seq_cst) != 0)
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
    else if(!strcmp(command,"timing get"))reply_timing(engine,client,client_size);
    else if(!strcmp(command,"timing reset")) {
        const char *reply = fxdsp_reset_timing(atomic_load_explicit(&engine->dsp,memory_order_acquire))
            ? "ok\n" : "error timing reset pending\n";
        (void)sendto(engine->control_fd,reply,strlen(reply),0,(const struct sockaddr *)client,client_size);
    }
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
    else if(!strcmp(command,"live abort") && fxdsp_live_abort(atomic_load_explicit(&engine->dsp,memory_order_acquire)))
        (void)sendto(engine->control_fd,"ok\n",3,0,(const struct sockaddr *)client,client_size);
    else if(!strcmp(command,"live commit")) {
        const char *reply = fxdsp_live_commit(atomic_load_explicit(&engine->dsp,memory_order_acquire))
            ? "ok\n" : "error live commit timeout\n";
        (void)sendto(engine->control_fd,reply,strlen(reply),0,(const struct sockaddr *)client,client_size);
    }
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

static void destroy_engine(struct engine *engine) {
    /* Control swaps and helper joins still need the context's RT registry. */
    stop_control(engine);
    /* pw_filter_destroy() deactivates the implementation node and removes its
     * processing source under the data loop's lock, so no callback can still
     * reach the graph before it or the helpers it borrows are released. */
    if (engine->filter) pw_filter_destroy(engine->filter);
    fxdsp_free(atomic_load(&engine->dsp));
    fx_workers_destroy(engine->workers);
    if (engine->core) pw_core_disconnect(engine->core);
    if (engine->context) pw_context_destroy(engine->context);
    if (engine->loop) pw_main_loop_destroy(engine->loop);
    pw_deinit();
}

static void on_process(void *data, struct spa_io_position *position) {
    struct engine *engine = data;
    /* seq_cst pairs with the writer's drain in fxdsp_swap_config so a reader
     * that loaded the previous graph keeps the writer from freeing it. */
    atomic_fetch_add_explicit(&engine->processing, 1, memory_order_seq_cst);
    fxdsp *dsp = atomic_load_explicit(&engine->dsp, memory_order_seq_cst);
    fxdsp_begin_callback(dsp);
    uint64_t started = fxdsp_timing_enabled(dsp) ? fx_workers_now_ns() : 0;
    uint32_t frames = position && position->clock.duration ? position->clock.duration : 1024;
    const float *input[FXDSP_MAX_CHANNELS];
    float *output[FXDSP_MAX_CHANNELS];
    float *post_effect[2];
    int complete = 1;
    for (unsigned i = 0; i < fxdsp_inputs(dsp); i++) {
        input[i] = pw_filter_get_dsp_buffer(engine->input[i], frames);
        if (!input[i] && i < 2U) complete = 0;
    }
    for (unsigned i = 0; i < fxdsp_outputs(dsp); i++) {
        /* A hardware output with fewer channels than the engine layout
         * leaves some output ports unlinked; those buffers are NULL and the
         * channels are skipped by fxdsp_process_tapped.  They must not
         * silence the whole engine and the post_effect meter taps. */
        output[i] = pw_filter_get_dsp_buffer(engine->output[i], frames);
    }
    for (unsigned i = 0; i < 2; i++) {
        post_effect[i] = pw_filter_get_dsp_buffer(engine->post_effect[i], frames);
    }
    if (!complete) {
        for (unsigned i = 0; i < fxdsp_outputs(dsp); i++)
            if (output[i]) memset(output[i], 0, frames * sizeof *output[i]);
        for (unsigned i = 0; i < 2; i++)
            if (post_effect[i]) memset(post_effect[i], 0, frames * sizeof *post_effect[i]);
        if (started) fxdsp_record_callback(dsp, fx_workers_now_ns() - started);
        atomic_fetch_sub_explicit(&engine->processing, 1, memory_order_seq_cst);
        return;
    }
    fxdsp_process_tapped(dsp, input, output,
                          post_effect[0] && post_effect[1] ? post_effect : NULL, frames);
    if (started) fxdsp_record_callback(dsp, fx_workers_now_ns() - started);
    atomic_fetch_sub_explicit(&engine->processing, 1, memory_order_seq_cst);
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
    atomic_init(&engine.dsp, initial_dsp);
    atomic_init(&engine.processing, 0);
    engine.loop = pw_main_loop_new(NULL);
    if (!engine.loop) {
        fprintf(stderr, "cannot create PipeWire main loop\n");
        destroy_engine(&engine);
        return 1;
    }
    engine.context = pw_context_new(pw_main_loop_get_loop(engine.loop),
                                    pw_properties_new(PW_KEY_APP_NAME, "fxroute-dsp", NULL), 0);
    if (engine.context) engine.core = pw_context_connect(engine.context, NULL, 0);
    if (!engine.core) {
        fprintf(stderr, "cannot connect PipeWire context\n");
        destroy_engine(&engine); return 1;
    }
    engine.thread_utils = pw_context_get_object(engine.context, SPA_TYPE_INTERFACE_ThreadUtils);
    engine.filter = pw_filter_new(engine.core, "fxroute-dsp",
        pw_properties_new(PW_KEY_MEDIA_TYPE, "Audio", PW_KEY_MEDIA_CATEGORY, "Filter",
                          PW_KEY_MEDIA_ROLE, "DSP", PW_KEY_NODE_NAME, "fxroute_dsp", NULL));
    if (!engine.filter) {
        fprintf(stderr, "cannot create PipeWire filter\n");
        destroy_engine(&engine);
        return 1;
    }
    pw_filter_add_listener(engine.filter, &engine.filter_listener, &filter_events, &engine);
    engine.workers = start_workers(&engine, initial_dsp);
    unsigned jobs = prepare_graph(&engine, initial_dsp);
    log_worker_status(&engine, jobs);
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
        destroy_engine(&engine);
        return 1;
    }
    if(argc==3&&start_control(&engine,argv[2])) {
        destroy_engine(&engine);
        return 1;
    }
    signal_engine = &engine;
    signal(SIGINT, stop_engine); signal(SIGTERM, stop_engine);
    pw_main_loop_run(engine.loop);
    destroy_engine(&engine);
    return 0;
}
