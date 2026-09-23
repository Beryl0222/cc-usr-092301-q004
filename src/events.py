"""不可变领域事件与事件存储。

事件信封沿用基线合同：``event_id / kind / occurred_at / subject_id / version``，
领域负载放在 ``payload`` 中。存储只负责追加与读取，不包含任何守门判断。
"""
from __future__ import annotations

import datetime as _dt
import itertools
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from .domain import Kind

SUBJECT = "clinical-agent-release-governance"


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: Kind
    occurred_at: _dt.datetime
    subject_id: str
    version: int
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "occurred_at": self.occurred_at.isoformat(),
            "subject_id": self.subject_id,
            "version": self.version,
            "payload": _jsonable(self.payload),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class EventStore:
    """单流追加存储；``version`` 是全局单调序号（从 1 开始）。"""

    def __init__(self, clock: "_Clock | None" = None):
        self._events: list[Event] = []
        self._clock = clock or SystemClock()
        self._seq = itertools.count(1)

    def now(self) -> _dt.datetime:
        return self._clock.now()

    def append(self, kind: Kind, payload: dict[str, Any]) -> Event:
        event = Event(
            event_id=f"{self._clock.day_stamp()}-{next(self._seq):04d}",
            kind=kind,
            occurred_at=self._clock.now(),
            subject_id=SUBJECT,
            version=len(self._events) + 1,
            payload=deepcopy(payload),
        )
        self._events.append(event)
        return event

    def all(self) -> list[Event]:
        return list(self._events)

    def replay(self, events: Iterable[Event]) -> None:
        """载入既有事件流（审计/重放视图用）。

        重放存储用于从历史事件还原状态与卷宗；在其上做新的写入不属于
        正常生命周期，调用方应只读取重放结果。
        """
        self._events.extend(events)

    def for_use(self, use_id: str) -> list[Event]:
        return [e for e in self._events if e.payload.get("use_id") == use_id]

    def for_release(self, release_id: str) -> list[Event]:
        return [e for e in self._events if e.payload.get("release_id") == release_id]

    def exposure_events(self, use_id: str | None = None) -> list[Event]:
        events = [e for e in self._events if e.kind == Kind.EXPOSURE_RECORDED]
        if use_id is not None:
            events = [e for e in events if e.payload["use_id"] == use_id]
        return events


class _Clock:
    def now(self) -> _dt.datetime:  # pragma: no cover - 接口
        raise NotImplementedError

    def day_stamp(self) -> str:
        return self.now().strftime("%m%d%y")


class SystemClock(_Clock):
    def now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.timezone.utc)


class FixedClock(_Clock):
    """测试/重放用固定时钟，可手动推进。"""

    def __init__(self, instant: _dt.datetime):
        self._instant = instant

    def now(self) -> _dt.datetime:
        return self._instant

    def advance(self, timedelta: _dt.timedelta) -> None:
        self._instant += timedelta

    def set(self, instant: _dt.datetime) -> None:
        self._instant = instant


def load(events: Iterable[Event]) -> dict[str, Any]:
    """把事件流折叠成供视图使用的只读快照。

    快照不是决策权威——守门判断每次都从当前快照读，但所有结论都可由
    事件流重放复现。
    """
    uses: dict[str, dict[str, Any]] = {}
    releases: dict[str, dict[str, Any]] = {}
    exposures: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    signals: dict[str, dict[str, Any]] = {}

    for event in events:
        p = event.payload
        kind = event.kind

        if kind == Kind.USE_REGISTERED:
            uses[p["use_id"]] = {
                **p,
                "status": "PENDING",
                "active_release_id": None,
                "active_since": None,
                "canary": None,
                "block_history": [],
                "suspensions": [],
                "recall": None,
                "adverse_events": [],
                "boundary_violations": [],
                "registered_version": event.version,
            }
        elif kind == Kind.USE_SCOPE_AMENDED:
            scope = dict(p["scope"])
            use = uses[p["use_id"]]
            use.update(scope)
            use.setdefault("amendments", []).append(
                {"by": p["by"], "reason": p["reason"], "scope": scope}
            )
        elif kind == Kind.USE_DEACTIVATED:
            uses[p["use_id"]]["status"] = "DEACTIVATED"
        elif kind == Kind.RELEASE_PLANNED:
            releases[p["release_id"]] = {
                **p,
                "frozen": False,
                "planned": {u["use_id"]: u for u in p["uses"]},
                "evidence": defaultdict(dict),
                "approvals": {},
                "activations": {},
                "planned_version": event.version,
            }
        elif kind == Kind.PLAN_FROZEN:
            releases[p["release_id"]]["frozen"] = True
        elif kind == Kind.VALIDATION_EVIDENCE_RECORDED:
            # 一次证据记录同时携带总体与全部关键亚组结果；重记时以最新为准，
            # 历史记录仍完整保留在事件流中。
            releases[p["release_id"]]["evidence"][p["use_id"]] = p
        elif kind == Kind.INDEPENDENT_APPROVAL_GRANTED:
            releases[p["release_id"]]["approvals"][p["use_id"]] = p
        elif kind == Kind.USE_ACTIVATED:
            release = releases[p["release_id"]]
            use = uses[p["use_id"]]
            release["activations"][p["use_id"]] = p
            use["status"] = "ACTIVE"
            use["active_release_id"] = p["release_id"]
            use["active_since"] = p["at"].isoformat()
            use["canary"] = p["canary"]
        elif kind == Kind.EXPOSURE_RECORDED:
            exposures[p["use_id"]].append(p)
        elif kind == Kind.DRIFT_SIGNAL_RAISED:
            signals[p["signal_id"]] = {**p, "triage": None}
        elif kind == Kind.DRIFT_TRIAGED:
            signals[p["signal_id"]]["triage"] = p
        elif kind == Kind.ADVERSE_EVENT_RECORDED:
            uses[p["use_id"]]["adverse_events"].append(p)
        elif kind == Kind.USE_SUSPENDED:
            use = uses[p["use_id"]]
            use["status"] = "SUSPENDED"
            use["active_release_id"] = None
            use["suspended_at_version"] = event.version
            use["suspensions"].append(p)
            use["block_history"].append(
                {"reason": p["reason"], "at": event.occurred_at.isoformat()}
            )
        elif kind == Kind.USE_RECALLED:
            use = uses[p["use_id"]]
            use["status"] = "RECALLED"
            use["active_release_id"] = None
            use["recall"] = p
            use["block_history"].append(
                {"reason": p["reason"], "at": event.occurred_at.isoformat()}
            )
        elif kind == Kind.PURPOSE_BOUNDARY_VIOLATION:
            uses[p["use_id"]]["boundary_violations"].append(p)

    return {"uses": uses, "releases": releases, "exposures": exposures, "signals": signals}
