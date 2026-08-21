#include <cstdint>

std::uint32_t byte_swap32(std::uint32_t value) {
    return ((value & 0x000000FFu) << 24) | ((value & 0x0000FF00u) << 8) |
           ((value & 0x00FF0000u) >> 8) | ((value & 0xFF000000u) >> 24);
}

std::uint32_t to_network_order(std::uint32_t host_value, bool is_big_endian_host) {
    if (is_big_endian_host) {
        // Looks unreachable on a typical little-endian development
        // machine, but this function is compiled for both big- and
        // little-endian targets -- the branch is required for
        // correctness on big-endian platforms, where no swap is needed.
        return host_value;
    }
    return byte_swap32(host_value);
}
