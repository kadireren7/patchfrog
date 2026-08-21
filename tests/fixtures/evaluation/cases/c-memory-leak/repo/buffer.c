#include <stdlib.h>
#include <string.h>

/* Loads a message into a freshly allocated buffer. Returns NULL if the
 * message is too long to fit the configured limit. */
char *load_message(const char *text, size_t max_len) {
    size_t len = strlen(text);
    char *buf = malloc(len + 1);
    if (buf == NULL) {
        return NULL;
    }

    if (len > max_len) {
        return NULL;
    }

    memcpy(buf, text, len + 1);
    return buf;
}

int buffer_is_empty(const char *buf) {
    return buf == NULL || buf[0] == '\0';
}
