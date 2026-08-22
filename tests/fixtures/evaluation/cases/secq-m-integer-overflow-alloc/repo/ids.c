#include <stdlib.h>

int *allocate_ids(unsigned int count) {
    /* count is taken directly from a network-supplied field. count *
     * sizeof(int) can wrap around on the allocation size calculation
     * if count is large enough, yielding a too-small allocation that
     * the caller then writes `count` elements into. */
    int *ids = malloc(count * sizeof(int));
    return ids;
}

int *allocate_fixed_ids(void) {
    return malloc(16 * sizeof(int));
}
