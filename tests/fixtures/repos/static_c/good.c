#include <stdlib.h>

int add(int a, int b)
{
    return a + b;
}

void free_properly(void)
{
    char *buf = malloc(64);
    if (buf != NULL)
    {
        buf[0] = 'x';
        free(buf);
    }
}
