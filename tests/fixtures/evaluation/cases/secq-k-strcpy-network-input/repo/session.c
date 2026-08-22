#include <string.h>

void handle_username_field(char *dest, const char *network_input) {
    /* network_input is copied directly from an incoming network packet
     * with no length limit before this call is made. */
    strcpy(dest, network_input);
}

void handle_fixed_banner(char *dest) {
    strcpy(dest, "welcome");
}
