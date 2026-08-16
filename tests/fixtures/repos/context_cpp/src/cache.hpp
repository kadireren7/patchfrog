#pragma once

class Cache {
public:
    void insert(int key, int value);
    int lookup(int key);

private:
    void evict();
};
