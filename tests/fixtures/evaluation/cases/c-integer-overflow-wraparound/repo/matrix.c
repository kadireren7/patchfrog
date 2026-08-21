#include <stdlib.h>
#include <string.h>

/* Allocates storage for `count` doubles. count comes from untrusted
 * input (e.g. a parsed file header), and count * sizeof(double) is
 * computed as an int, which can wrap around to a small or negative
 * value for a large count -- malloc then under-allocates, and the
 * caller's subsequent writes of `count` doubles overflow the buffer. */
double *allocate_row(int count) {
    int byte_size = count * (int)sizeof(double);
    double *row = malloc((size_t)byte_size);
    if (row == NULL) {
        return NULL;
    }
    memset(row, 0, (size_t)byte_size);
    return row;
}

int row_is_valid(const double *row) {
    return row != NULL;
}
