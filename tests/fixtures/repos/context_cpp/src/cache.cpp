#include "cache.hpp"

void Cache::evict() {
    // drop the oldest entry
}

void Cache::insert(int key, int value) {
    evict();
}

int Cache::lookup(int key) {
    return key;
}

int unrelated_function() {
    return 42;
}
