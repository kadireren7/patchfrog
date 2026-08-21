#include <stdio.h>

#include "parse.c"

void print_entry(const char *line) {
    /* parse_line's documented contract requires a NULL check before use
     * -- it is missing here, so a malformed line dereferences a NULL
     * Entry*. */
    Entry *entry = parse_line(line);
    printf("%s => %s\n", entry->key, entry->value);
}
