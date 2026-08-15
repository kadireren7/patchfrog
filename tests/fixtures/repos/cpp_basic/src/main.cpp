#include "cache.hpp"

int main()
{
    patchfrog::Cache cache;

    cache.set(0, 42);
    return cache.get(0);
}
