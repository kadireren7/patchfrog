#include <stdexcept>

class ConfigError : public std::runtime_error {
public:
    explicit ConfigError(const char *what) : std::runtime_error(what) {}
};

int parse_port(const char *raw) {
    try {
        return std::stoi(raw);
    } catch (const std::invalid_argument &) {
        // Deliberately narrow: only a malformed (non-numeric) port
        // string is translated into this service's own ConfigError
        // type, so callers only ever need to catch one exception
        // hierarchy. std::out_of_range is intentionally left to
        // propagate as-is, since that indicates a numeric-but-
        // nonsensical value the caller should see the original detail
        // for.
        throw ConfigError("invalid port string");
    }
}
