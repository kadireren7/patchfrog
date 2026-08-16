#include "list.h"

void process_request(struct node_list *list, int value) {
    list_insert(list, value);
}
