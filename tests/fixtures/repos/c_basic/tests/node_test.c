#include "../src/node.h"

int main(void)
{
    t_node *n = node_new((void *)0);

    return n == 0;
}
