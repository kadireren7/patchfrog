#include <mutex>

class RequestCounter {
public:
    /* Correctly locks before mutating the shared counter. */
    void record_success() {
        std::lock_guard<std::mutex> guard(mutex_);
        success_count_++;
    }

    /* Mutates the same shared counter state, but forgets to take the
     * lock -- a data race against record_success() from other
     * threads. */
    void record_failure() {
        failure_count_++;
    }

    int success_count() const { return success_count_; }
    int failure_count() const { return failure_count_; }

private:
    std::mutex mutex_;
    int success_count_ = 0;
    int failure_count_ = 0;
};
