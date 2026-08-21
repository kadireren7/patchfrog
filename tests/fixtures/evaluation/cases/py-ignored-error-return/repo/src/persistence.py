class Document:
    def __init__(self, dirty: bool = True) -> None:
        self.dirty = dirty

    def save(self) -> bool:
        """Persist the document. Returns False if saving failed (e.g. disk
        full); callers must check this before assuming the save succeeded."""
        if self._disk_has_space():
            self.dirty = False
            return True
        return False

    def _disk_has_space(self) -> bool:
        return True  # simplified for the fixture


def save_and_notify(document: Document, notifier) -> None:
    document.save()
    notifier("document saved")


def save_or_raise(document: Document, notifier) -> None:
    if not document.save():
        raise RuntimeError("save failed")
    notifier("document saved")
