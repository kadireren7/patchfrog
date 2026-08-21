#include "widget_factory.cpp"

double total_weight_for_batch(int count) {
    double total = 0.0;
    for (int i = 0; i < count; i++) {
        Widget *w = create_widget(i, 1.5);
        total += w->weight;
        // create_widget's documented ownership contract requires
        // `delete w;` here -- it is missing, leaking one Widget per
        // iteration.
    }
    return total;
}
