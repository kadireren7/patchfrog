#include <stdlib.h>
#include <string.h>

struct session {
    char *token;
};

/* Tears down a session. On a corrupt (too-short) token, the cleanup
 * path frees session->token and then falls through to the normal
 * cleanup below, which frees it again. */
void close_session(struct session *s) {
    if (s == NULL) {
        return;
    }

    if (s->token != NULL && strlen(s->token) < 8) {
        free(s->token);
    }

    free(s->token);
    s->token = NULL;
}

int session_has_token(const struct session *s) {
    return s != NULL && s->token != NULL;
}
