/* SPDX-License-Identifier: GPL-3.0-or-later
 * The pinned engine has no thread-count API. Interpose its CPU-count query
 * only in the recognition child, never in Cassini's GTK process.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <unistd.h>

static long (*system_query)(int);
__attribute__((constructor)) static void prepare_query(void) {
    system_query = (long (*)(int))dlsym(RTLD_NEXT, "sysconf");
}

long sysconf(int option) {
    if (!system_query) { errno = ENOSYS; return -1; }
    long actual = system_query(option);
    if (option == _SC_NPROCESSORS_CONF || option == _SC_NPROCESSORS_ONLN) {
        const char *value = getenv("CASSINI_WHISTLE_THREADS");
        if (value && *value) {
            char *tail;
            long limit = strtol(value, &tail, 10);
            if (!*tail && limit >= 1 && limit <= 32 && actual > limit) return limit;
        }
    }
    return actual;
}
