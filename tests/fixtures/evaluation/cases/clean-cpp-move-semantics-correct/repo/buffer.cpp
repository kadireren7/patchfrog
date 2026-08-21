#include <cstddef>

class Buffer {
public:
    explicit Buffer(std::size_t size) : data_(new int[size]), size_(size) {}

    Buffer(Buffer &&other) noexcept : data_(other.data_), size_(other.size_) {
        other.data_ = nullptr;
        other.size_ = 0;
    }

    Buffer &operator=(Buffer &&other) noexcept {
        if (this != &other) {
            delete[] data_;
            data_ = other.data_;
            size_ = other.size_;
            other.data_ = nullptr;
            other.size_ = 0;
        }
        return *this;
    }

    Buffer(const Buffer &) = delete;
    Buffer &operator=(const Buffer &) = delete;

    ~Buffer() { delete[] data_; }

    std::size_t size() const { return size_; }

private:
    int *data_;
    std::size_t size_;
};
