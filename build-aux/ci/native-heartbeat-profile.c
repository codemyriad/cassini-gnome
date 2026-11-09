#define _GNU_SOURCE
#include <signal.h>
#include <time.h>
#include <execinfo.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <fcntl.h>
#include <stdio.h>
static void *samples[4096][48];
static int sizes[4096], phases[4096];
static volatile sig_atomic_t current_phase;
void profile_phase(int phase) { current_phase=phase; }
static volatile sig_atomic_t count;
static timer_t timer;
static void sample(int sig) {
    int i = count;
    if (i < 4096) { phases[i]=current_phase; sizes[i] = backtrace(samples[i], 48); count = i + 1; }
}
void profile_start(void) {
    void *warmup[48]; backtrace(warmup, 48); count=0;
    struct sigaction action={0}; action.sa_handler=sample; action.sa_flags=SA_RESTART; sigemptyset(&action.sa_mask); sigaction(SIGPROF,&action,0);
    struct sigevent event={0}; event.sigev_notify=SIGEV_THREAD_ID; event.sigev_signo=SIGPROF; event._sigev_un._tid=syscall(SYS_gettid);
    timer_create(CLOCK_THREAD_CPUTIME_ID,&event,&timer);
    struct itimerspec spec={.it_interval={0,1000000},.it_value={0,1000000}};
    timer_settime(timer,0,&spec,0);
}
void profile_stop(const char *path) {
    timer_delete(timer);
    int fd=open(path,O_WRONLY|O_CREAT|O_TRUNC,0600);
    for(int i=0;i<count;i++) { dprintf(fd,"SAMPLE phase %d\n",phases[i]); backtrace_symbols_fd(samples[i],sizes[i],fd); }
    close(fd);
}
