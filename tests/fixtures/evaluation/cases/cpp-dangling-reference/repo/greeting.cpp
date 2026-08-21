#include <string>

/* Builds a greeting string on the stack and returns a reference to
 * it. The local `greeting` is destroyed when the function returns,
 * so the returned reference is immediately dangling. */
const std::string &build_greeting(const std::string &name) {
    std::string greeting = "Hello, " + name + "!";
    return greeting;
}

std::string build_farewell(const std::string &name) {
    return "Goodbye, " + name + "!";
}
