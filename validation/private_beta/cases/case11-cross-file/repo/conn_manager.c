#include "hiredis.h"

extern int reconnectWithBackoff(int (*tryConnect)(void), int max_total_attempts);

static int tryConnectToRedis(void) {
    redisContext *c = redisConnect("127.0.0.1", 6379);
    if (c == NULL || c->err) {
        if (c) redisFree(c);
        return 0;
    }
    redisFree(c);
    return 1;
}

/* Reconnects to the local Redis instance on startup, retrying
 * persistently since a fresh deploy's Redis container may still be
 * starting up. */
int connectOnStartup(void) {
    return reconnectWithBackoff(tryConnectToRedis, 20);
}
