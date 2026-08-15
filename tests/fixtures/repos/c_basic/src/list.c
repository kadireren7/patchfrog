#include "node.h"

void list_push(t_node **head, void *content)
{
    t_node *node = node_new(content);

    node->next = *head;
    *head = node;
}
