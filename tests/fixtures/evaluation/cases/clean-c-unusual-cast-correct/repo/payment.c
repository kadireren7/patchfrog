#include <stddef.h>

typedef struct {
    int id;
    double amount;
} Payment;

void process_callback(void *context) {
    /* This callback is only ever registered via
     * register_payment_handler() below, which always passes a
     * Payment* -- the cast is safe by construction, not a type-punning
     * bug. */
    Payment *payment = (Payment *)context;
    payment->amount += 1.0;
}

void register_payment_handler(Payment *payment, void (*handler)(void *)) {
    handler(payment);
}
