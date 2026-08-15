#include <memory>
#include <string>
#include <utility>

int null_deref()
{
    int *p = nullptr;
    return *p;
}

void resource_leak()
{
    int *buf = new int[64];
    buf[0] = 1;
}

std::string use_after_move(std::string s)
{
    std::string moved = std::move(s);
    return s;
}
