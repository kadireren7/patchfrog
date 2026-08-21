#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct record {
    char *name;
    int code;
};

/* Frees the record's name, then logs it via a debug print that reads
 * name after it has already been freed. */
void discard_record(struct record *r) {
    free(r->name);
    printf("discarded record: %s (code=%d)\n", r->name, r->code);
    r->name = NULL;
}

int record_code(const struct record *r) {
    return r->code;
}
