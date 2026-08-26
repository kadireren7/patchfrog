#ifndef RAPIDJSON_PARSECACHE_H_
#define RAPIDJSON_PARSECACHE_H_

// Small cache of recently-parsed document sizes, shared across worker
// threads that all parse JSON from the same connection pool. Private
// beta validation sprint fixture -- not part of the real rapidjson
// project.

#include <thread>

namespace rapidjson {

class ParseSizeCache {
public:
    ParseSizeCache() : count_(0), totalBytes_(0) {}

    // Called from multiple worker threads after each successful parse.
    void RecordParse(size_t bytes) {
        count_++;
        totalBytes_ += bytes;
    }

    double AverageBytes() const {
        if (count_ == 0) return 0.0;
        return static_cast<double>(totalBytes_) / static_cast<double>(count_);
    }

private:
    size_t count_;
    size_t totalBytes_;
};

} // namespace rapidjson

#endif
