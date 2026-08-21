#include <stddef.h>

int sum_array(const int *values, size_t n) {
    int total = 0;
    for (size_t i = 0; i < n; i++) {
        total += values[i];
    }
    return total;
}

void fill_zero(int *values, size_t n) {
    for (size_t i = 0; i < n; i++) {
        values[i] = 0;
    }
}
