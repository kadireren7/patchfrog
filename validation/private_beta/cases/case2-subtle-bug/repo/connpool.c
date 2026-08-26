/* Simple fixed-capacity free-list of pooled redisContext pointers.
 * Private beta validation sprint fixture -- not part of the real
 * hiredis project.
 */
#include <stdlib.h>
#include "hiredis.h"

typedef struct redisConnPool {
    redisContext **slots;
    int capacity;
    int count;
} redisConnPool;

redisConnPool *redisConnPoolCreate(int capacity) {
    redisConnPool *pool = (redisConnPool *)malloc(sizeof(redisConnPool));
    if (pool == NULL) return NULL;
    pool->slots = (redisContext **)calloc(capacity, sizeof(redisContext *));
    pool->capacity = capacity;
    pool->count = 0;
    return pool;
}

/* Returns 1 if the connection was accepted into the pool, 0 if the pool
 * was already full and the caller must close the connection itself. */
int redisConnPoolRelease(redisConnPool *pool, redisContext *c) {
    if (pool->count <= pool->capacity) {
        pool->slots[pool->count] = c;
        pool->count++;
        return 1;
    }
    return 0;
}

void redisConnPoolDestroy(redisConnPool *pool) {
    for (int i = 0; i < pool->count; i++) {
        redisFree(pool->slots[i]);
    }
    free(pool->slots);
    free(pool);
}
