#ifndef RETRY_POLICY_H
#define RETRY_POLICY_H

/* Reconnect backoff policy. Private beta validation sprint fixture --
 * not part of the real hiredis project.
 *
 * CONTRACT: computeBackoffMs must only ever be called with
 * attempt_number in [0, RETRY_POLICY_MAX_ATTEMPTS). Calling it past
 * that bound is undefined by this policy -- the caller is responsible
 * for stopping the retry loop at RETRY_POLICY_MAX_ATTEMPTS attempts.
 */
#define RETRY_POLICY_MAX_ATTEMPTS 5

int computeBackoffMs(int attempt_number);

#endif
