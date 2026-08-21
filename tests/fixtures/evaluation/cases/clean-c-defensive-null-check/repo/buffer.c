#include <stdlib.h>

typedef struct {
    int *data;
    size_t count;
} Buffer;

Buffer *buffer_create(size_t count) {
    Buffer *buf = malloc(sizeof(Buffer));
    if (buf == NULL) {
        return NULL;
    }
    buf->data = malloc(count * sizeof(int));
    if (buf->data == NULL) {
        free(buf);
        return NULL;
    }
    buf->count = count;
    return buf;
}

void buffer_destroy(Buffer *buf) {
    if (buf == NULL) {
        return;
    }
    free(buf->data);
    free(buf);
}
