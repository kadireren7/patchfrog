#include "retry_policy.h"

/* Exponential backoff: 2^attempt_number * 50ms. Only valid for
 * attempt_number in [0, RETRY_POLICY_MAX_ATTEMPTS) -- see the CONTRACT
 * note in retry_policy.h. Left shift on attempt_number is only safe
 * within that bound; a caller that keeps retrying past it will shift
 * into undefined/overflowing territory. */
int computeBackoffMs(int attempt_number) {
    return (1 << attempt_number) * 50;
}
