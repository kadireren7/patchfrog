#include "list.h"

static void list_evict(struct node_list *list) {
    list->head = list->head->next;
}

void list_insert(struct node_list *list, int value) {
    if (list->count > 100) {
        list_evict(list);
    }
    list->count++;
}

int unrelated_function(void) {
    return 42;
}
