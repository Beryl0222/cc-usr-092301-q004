"""仅追加事件存储与重放。

内存实现使用有序列表；append 时强制信封/负载合同、幂等 event_id 与
``version`` 单调（同一 subject_id 内）。投影通过 ``replay`` 得到。
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock

from .events import require_valid


class EventStore:
    def __init__(self) -> None:
        self._events: list[dict] = []
        self._seen_ids: set[str] = set()
        self._versions: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    def append(self, record: dict) -> None:
        require_valid(record)
        event_id = record["event_id"]
        subject = record["subject_id"]
        version = record["version"]
        with self._lock:
            if event_id in self._seen_ids:
                raise ValueError(f"duplicate event_id: {event_id}")
            if version != self._versions[subject] + 1:
                raise ValueError(
                    f"version conflict for {subject!r}: "
                    f"expected {self._versions[subject] + 1}, got {version}"
                )
            self._events.append(dict(record))
            self._seen_ids.add(event_id)
            self._versions[subject] = version

    def replay(self, subject_id: str | None = None) -> list[dict]:
        with self._lock:
            events = [dict(e) for e in self._events]
        if subject_id is not None:
            events = [e for e in events if e["subject_id"] == subject_id]
        return events

    def next_version(self, subject_id: str) -> int:
        with self._lock:
            return self._versions[subject_id] + 1

    def count(self) -> int:
        with self._lock:
            return len(self._events)
