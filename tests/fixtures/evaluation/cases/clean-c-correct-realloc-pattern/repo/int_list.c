#include <stdlib.h>

typedef struct {
    int *items;
    size_t count;
    size_t capacity;
} IntList;

int int_list_push(IntList *list, int value) {
    if (list->count == list->capacity) {
        size_t new_capacity = list->capacity == 0 ? 4 : list->capacity * 2;
        int *tmp = realloc(list->items, new_capacity * sizeof(int));
        if (tmp == NULL) {
            /* realloc failure: list->items is still valid and unfreed
             * here, since we assigned the result to tmp, not
             * list->items -- returning failure without touching
             * list->items avoids leaking or corrupting the existing
             * buffer. */
            return -1;
        }
        list->items = tmp;
        list->capacity = new_capacity;
    }
    list->items[list->count++] = value;
    return 0;
}
