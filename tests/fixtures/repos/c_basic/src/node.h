#ifndef NODE_H
#define NODE_H

typedef struct s_node
{
    void *content;
    struct s_node *next;
} t_node;

t_node *node_new(void *content);

#endif
