#include <string.h>

void copy_display_name(char *dst, const char *src) {
    /* strcpy performs no bounds checking -- if src is longer than dst's
     * buffer, this overflows it. Semgrep's bundled
     * patchfrog-c-unsafe-strcpy rule catches this pattern without any
     * AI call. */
    strcpy(dst, src);
}

int compute_length(const char *s) {
    return (int)strlen(s);
}
