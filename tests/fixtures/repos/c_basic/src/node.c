#include "node.h"
#include <stdlib.h>

t_node *node_new(void *content)
{
    t_node *node = malloc(sizeof(t_node));

    node->content = content;
    node->next = NULL;
    return node;
}
