#include <cstdio>

class FileHandle {
public:
    explicit FileHandle(const char *path) : file_(std::fopen(path, "r")) {}

    ~FileHandle() {
        if (file_ != nullptr) {
            std::fclose(file_);
        }
    }

    FileHandle(const FileHandle &) = delete;
    FileHandle &operator=(const FileHandle &) = delete;

    bool is_open() const { return file_ != nullptr; }

private:
    std::FILE *file_;
};

bool file_exists(const char *path) {
    FileHandle handle(path);
    return handle.is_open();
}
