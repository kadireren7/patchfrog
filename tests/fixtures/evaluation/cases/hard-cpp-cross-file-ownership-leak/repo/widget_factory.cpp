struct Widget {
    int id;
    double weight;
};

// Ownership contract: the caller takes ownership of the returned
// Widget* and is responsible for calling `delete` on it exactly once.
Widget *create_widget(int id, double weight) {
    Widget *w = new Widget();
    w->id = id;
    w->weight = weight;
    return w;
}
