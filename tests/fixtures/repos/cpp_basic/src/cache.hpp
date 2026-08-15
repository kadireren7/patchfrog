#pragma once

namespace patchfrog
{

class Cache
{
public:
    int get(int key);
    void set(int key, int value);

private:
    int store_[16];
};

}
