#include "cache.hpp"

namespace patchfrog
{

int Cache::get(int key)
{
    return store_[key];
}

void Cache::set(int key, int value)
{
    store_[key] = value;
}

}
