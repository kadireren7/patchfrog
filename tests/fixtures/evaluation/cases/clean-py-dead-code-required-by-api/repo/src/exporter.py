from abc import ABC, abstractmethod


class Exporter(ABC):
    @abstractmethod
    def export(self, rows: list[dict]) -> str: ...


class CsvExporter(Exporter):
    def export(self, rows: list[dict]) -> str:
        if not rows:
            # Required by Exporter's documented contract: callers rely on
            # getting an empty string (not an exception) for an empty
            # dataset, since export() output is piped directly to a file
            # write with no separate "was there data" check.
            return ""
        header = ",".join(rows[0].keys())
        lines = [header] + [",".join(str(v) for v in row.values()) for row in rows]
        return "\n".join(lines)
