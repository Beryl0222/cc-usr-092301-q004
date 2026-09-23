"""事件信封合同。

所有领域事件共用同一个信封（与基线 contracts/event.schema.json 对齐），
业务负载统一放在 ``payload`` 字段内。事件一经写入仅可追加，不可修改或删除。
"""

from __future__ import annotations

from datetime import datetime

REQUIRED = ("event_id", "kind", "occurred_at", "subject_id", "version")

# 信封字段以外的内容一律进入 payload，避免业务字段污染信封。
ENVELOPE = frozenset(REQUIRED) | {"payload", "actor", "metadata"}


def validate(record: dict) -> list[str]:
    """返回事件记录存在的问题列表；空列表表示通过。"""
    problems = [name for name in REQUIRED if name not in record]
    if not problems:
        if not isinstance(record["event_id"], str) or not record["event_id"].strip():
            problems.append("event_id_empty")
        if not isinstance(record["kind"], str) or not record["kind"].strip():
            problems.append("kind_empty")
        if not isinstance(record["subject_id"], str) or not record["subject_id"].strip():
            problems.append("subject_id_empty")
        if not isinstance(record["version"], int) or isinstance(record["version"], bool):
            problems.append("version_not_integer")
        elif record["version"] < 0:
            problems.append("version_negative")
        try:
            parse_timestamp(record["occurred_at"])
        except (TypeError, ValueError):
            problems.append("occurred_at_not_iso8601")
    unknown = set(record) - ENVELOPE
    if unknown:
        problems.append("unknown_fields:" + ",".join(sorted(unknown)))
    return problems


def parse_timestamp(value: str) -> datetime:
    """解析 ISO 8601 时间戳，要求携带时区（禁止朴素时间，避免跨院部歧义）。"""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must carry a timezone")
    return parsed
