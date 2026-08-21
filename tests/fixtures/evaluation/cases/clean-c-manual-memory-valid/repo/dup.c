#include <stdlib.h>
#include <string.h>

char *duplicate_upper(const char *input) {
    size_t len = strlen(input);
    char *copy = malloc(len + 1);
    if (copy == NULL) {
        return NULL;
    }
    for (size_t i = 0; i < len; i++) {
        char c = input[i];
        if (c >= 'a' && c <= 'z') {
            c = (char)(c - 'a' + 'A');
        }
        copy[i] = c;
    }
    copy[len] = '\0';
    return copy;
}

void free_duplicate(char *copy) {
    free(copy);
}
