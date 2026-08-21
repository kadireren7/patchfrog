#include <stdio.h>
#include <stdlib.h>

/* Opens the config file and reads its first line into out_line. If
 * the file is opened but fgets() fails (empty/unreadable file), the
 * function returns early without closing fp, leaking the handle. */
int load_first_line(const char *path, char *out_line, size_t out_size) {
    FILE *fp = fopen(path, "r");
    if (fp == NULL) {
        return -1;
    }

    if (fgets(out_line, (int)out_size, fp) == NULL) {
        return -1;
    }

    fclose(fp);
    return 0;
}

int file_exists(const char *path) {
    FILE *fp = fopen(path, "r");
    if (fp == NULL) {
        return 0;
    }
    fclose(fp);
    return 1;
}
