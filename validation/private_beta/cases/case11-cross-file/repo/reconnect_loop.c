#include <unistd.h>
#include "retry_policy.h"

/* Attempts to reconnect, sleeping between attempts according to the
 * configured backoff policy. Returns 1 if a caller-supplied tryConnect
 * eventually succeeds, 0 if every attempt failed. */
int reconnectWithBackoff(int (*tryConnect)(void), int max_total_attempts) {
    for (int attempt = 0; attempt < max_total_attempts; attempt++) {
        if (tryConnect()) {
            return 1;
        }
        int backoff_ms = computeBackoffMs(attempt);
        usleep(backoff_ms * 1000);
    }
    return 0;
}
