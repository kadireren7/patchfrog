#include <stdio.h>
#include <stdlib.h>

typedef struct {
    char *token;
} Session;

void invalidate_session(Session *s) {
    free(s->token);
}

void log_session_token(Session *s) {
    invalidate_session(s);
    printf("token was: %s\n", s->token);
}
