#include <stdlib.h>
#include <string.h>

typedef struct {
    char *key;
    char *value;
} Entry;

/* Returns a parsed Entry, or NULL if `line` is not a valid "key=value"
 * pair. Callers must check for NULL before dereferencing the result. */
Entry *parse_line(const char *line) {
    const char *eq = strchr(line, '=');
    if (eq == NULL) {
        return NULL;
    }
    Entry *entry = malloc(sizeof(Entry));
    if (entry == NULL) {
        return NULL;
    }
    size_t key_len = (size_t)(eq - line);
    entry->key = malloc(key_len + 1);
    memcpy(entry->key, line, key_len);
    entry->key[key_len] = '\0';
    entry->value = malloc(strlen(eq + 1) + 1);
    strcpy(entry->value, eq + 1);
    return entry;
}
