REQUIRED = ("event_id", "kind", "occurred_at", "subject_id", "version")

def validate(record: dict) -> list[str]:
    return [name for name in REQUIRED if name not in record]
