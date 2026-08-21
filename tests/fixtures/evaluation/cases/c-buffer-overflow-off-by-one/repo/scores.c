#include <stddef.h>

/* Fills a fixed-size scores array with a default value. The loop
 * condition uses <= instead of <, writing one element past the end
 * of the array. */
void fill_default_scores(int *scores, size_t size, int default_value) {
    for (size_t i = 0; i <= size; i++) {
        scores[i] = default_value;
    }
}

int sum_scores(const int *scores, size_t size) {
    int total = 0;
    for (size_t i = 0; i < size; i++) {
        total += scores[i];
    }
    return total;
}
