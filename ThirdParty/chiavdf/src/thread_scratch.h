#ifndef CHIAVDF_THREAD_SCRATCH_H
#define CHIAVDF_THREAD_SCRATCH_H

#include <stdexcept>

// MinGW's emulated TLS storage can be released before its C++ destructors.
// The adapter owns each scratch object on the stack; TLS contains only a
// non-owning pointer, whose destruction does not access released storage.
template<class T>
class chiavdf_scratch_scope {
public:
    inline static thread_local T* current = nullptr;

    chiavdf_scratch_scope() : previous_(current) { current = &value_; }
    ~chiavdf_scratch_scope() { current = previous_; }
    chiavdf_scratch_scope(const chiavdf_scratch_scope&) = delete;
    chiavdf_scratch_scope& operator=(const chiavdf_scratch_scope&) = delete;

private:
    T* previous_;
    T value_;
};

template<class T>
inline T& chiavdf_thread_scratch() {
#if defined(__MINGW32__)
    if (!chiavdf_scratch_scope<T>::current)
        throw std::logic_error("VDF scratch scope is missing");
    return *chiavdf_scratch_scope<T>::current;
#else
    static thread_local T value;
    return value;
#endif
}

#endif // CHIAVDF_THREAD_SCRATCH_H
