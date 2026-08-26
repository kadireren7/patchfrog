/* Builds a formatted "SET key value" command buffer for callers that
 * need the raw wire bytes ahead of time (e.g. for logging).
 * Private beta validation sprint fixture -- not part of the real
 * hiredis project.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Returns a newly malloc'd, NUL-terminated buffer containing the
 * formatted command, or NULL on allocation failure. Caller owns the
 * returned buffer. */
char *buildSetCommand(const char *key, const char *value) {
    size_t key_len = strlen(key);
    size_t value_len = strlen(value);
    size_t total_len = key_len + value_len + 32;

    char *buf = (char *)malloc(total_len);
    if (buf == NULL) {
        return NULL;
    }

    int written = snprintf(buf, total_len, "SET %s %s\r\n", key, value);
    if (written < 0 || (size_t)written >= total_len) {
        /* formatting failed or was truncated -- caller must not use buf */
        return NULL;
    }

    return buf;
}
