struct node {
    int value;
    struct node *next;
};

struct node_list {
    struct node *head;
    int count;
};

void list_insert(struct node_list *list, int value);
