#include <pthread.h>

typedef struct {
    pthread_mutex_t lock;
    int value;
} Counter;

int counter_increment_and_get(Counter *counter) {
    pthread_mutex_lock(&counter->lock);
    counter->value += 1;
    int result = counter->value;
    pthread_mutex_unlock(&counter->lock);
    return result;
}

int counter_get_if_positive(Counter *counter) {
    pthread_mutex_lock(&counter->lock);
    if (counter->value <= 0) {
        pthread_mutex_unlock(&counter->lock);
        return -1;
    }
    int result = counter->value;
    pthread_mutex_unlock(&counter->lock);
    return result;
}
