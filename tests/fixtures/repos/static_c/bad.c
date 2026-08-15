#include <stdlib.h>
#include <string.h>

int null_deref(void)
{
    int *p = NULL;
    return *p;
}

void memory_leak(void)
{
    char *buf = malloc(64);
    buf[0] = 'x';
}

void out_of_bounds(void)
{
    int arr[5];
    arr[10] = 1;
}

void unsafe_copy(char *dst, const char *src)
{
    strcpy(dst, src);
}
