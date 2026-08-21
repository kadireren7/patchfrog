#include <string>

struct Token {
    std::string text;
    int kind;
};

/* Builds a Token on the stack and returns a pointer to it. The Token
 * is destroyed when make_end_token returns, so the caller is left
 * holding a dangling pointer. */
Token *make_end_token() {
    Token end_token{"<end>", 0};
    return &end_token;
}

int token_kind(const Token *t) {
    return t->kind;
}
