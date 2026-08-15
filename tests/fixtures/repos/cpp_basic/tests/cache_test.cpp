#include "../src/cache.hpp"

int main()
{
    patchfrog::Cache c;

    c.set(1, 2);
    return c.get(1) == 2 ? 0 : 1;
}
