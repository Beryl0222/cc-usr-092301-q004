"""领域事件类型、负载合同与去标识化边界。

事件是本服务唯一的事实来源：登记、证据、审批、暴露、漂移、阻断、回滚、召回
全部表现为仅追加事件。任何事件负载都不得携带患者身份信息——病例只允许以
院内去标识病例号 ``case_ref`` 出现，且只能用于登记时声明的授权目的。
"""

from __future__ import annotations

from .contract import validate as validate_envelope

# ── 事件种类 ────────────────────────────────────────────────────────────────

# 用途登记：边界、科室人群、风险分级、阈值、复核职责、最小样本、数据授权目的
PURPOSE_REGISTERED = "PURPOSE_REGISTERED"
# 版本候选：模型/提示词/知识库/队列数据四个版本指纹缺一不可
VERSION_CANDIDATE_REGISTERED = "VERSION_CANDIDATE_REGISTERED"
# 验证证据提交与闸门结论
VALIDATION_SUBMITTED = "VALIDATION_SUBMITTED"
VALIDATION_PASSED = "VALIDATION_PASSED"
VALIDATION_FAILED = "VALIDATION_FAILED"
# 委员会决定（高风险用途须独立批准）
COMMITTEE_DECISION = "COMMITTEE_DECISION"
# 上线窗口与灰度
ROLLOUT_SCHEDULED = "ROLLOUT_SCHEDULED"
CANARY_STARTED = "CANARY_STARTED"
USE_ACTIVATED = "USE_ACTIVATED"
# 在线暴露记账：每条建议对应医生签字；覆盖建议须记录理由
EXPOSURE_RECORDED = "EXPOSURE_RECORDED"
# 漂移信号与三分类结论
DRIFT_SIGNAL_RAISED = "DRIFT_SIGNAL_RAISED"
DRIFT_CLASSIFIED = "DRIFT_CLASSIFIED"
# 最小半径阻断 / 人工兜底升级 / 回滚 / 不良事件 / 召回
USE_BLOCKED = "USE_BLOCKED"
HUMAN_BACKUP_ENFORCED = "HUMAN_BACKUP_ENFORCED"
ROLLBACK_INITIATED = "ROLLBACK_INITIATED"
ROLLBACK_COMPLETED = "ROLLBACK_COMPLETED"
ADVERSE_EVENT_REPORTED = "ADVERSE_EVENT_REPORTED"
RECALL_DECIDED = "RECALL_DECIDED"
USE_SUPERSEDED = "USE_SUPERSEDED"

ALL_KINDS = frozenset(
    {
        PURPOSE_REGISTERED,
        VERSION_CANDIDATE_REGISTERED,
        VALIDATION_SUBMITTED,
        VALIDATION_PASSED,
        VALIDATION_FAILED,
        COMMITTEE_DECISION,
        ROLLOUT_SCHEDULED,
        CANARY_STARTED,
        USE_ACTIVATED,
        EXPOSURE_RECORDED,
        DRIFT_SIGNAL_RAISED,
        DRIFT_CLASSIFIED,
        USE_BLOCKED,
        HUMAN_BACKUP_ENFORCED,
        ROLLBACK_INITIATED,
        ROLLBACK_COMPLETED,
        ADVERSE_EVENT_REPORTED,
        RECALL_DECIDED,
        USE_SUPERSEDED,
    }
)

# 每种事件负载的必填字段。
REQUIRED_PAYLOAD = {
    PURPOSE_REGISTERED: (
        "use_id", "agent", "intended_use", "risk_class",
        "scope", "key_subgroups", "thresholds",
        "review_mode", "reviewer_role", "data_purpose",
        "min_sample", "min_subgroup_sample",
    ),
    VERSION_CANDIDATE_REGISTERED: (
        "release_id", "use_ids", "model_version", "prompt_version",
        "knowledge_base_version", "dataset_version", "change_summary",
    ),
    VALIDATION_SUBMITTED: (
        "release_id", "use_id", "cohort_id", "dataset_version",
        "dataset_purpose", "sample_size", "overall_metrics",
        "subgroup_results", "leakage_screen_clear", "device_protocol",
        "submitted_by",
    ),
    VALIDATION_PASSED: ("release_id", "use_id"),
    VALIDATION_FAILED: ("release_id", "use_id", "reasons"),
    COMMITTEE_DECISION: (
        "release_id", "use_id", "decision", "approver_id",
        "independent", "rationale",
    ),
    ROLLOUT_SCHEDULED: (
        "release_id", "use_ids", "window_start", "window_end", "canary_scope",
    ),
    CANARY_STARTED: ("release_id", "use_ids", "canary_scope"),
    USE_ACTIVATED: ("release_id", "use_id", "scope"),
    EXPOSURE_RECORDED: (
        "release_id", "use_id", "scope", "case_ref", "recommendation_ref",
        "signoff_physician_id",
    ),
    DRIFT_SIGNAL_RAISED: (
        "signal_id", "release_id", "use_id", "scope", "metric",
        "observed", "expected",
    ),
    DRIFT_CLASSIFIED: ("signal_id", "release_id", "use_id", "scope", "category"),
    USE_BLOCKED: ("release_id", "use_id", "scope", "reasons"),
    HUMAN_BACKUP_ENFORCED: ("release_id", "use_id", "scope", "reason"),
    ROLLBACK_INITIATED: ("release_id", "use_ids", "reason", "affected_case_refs"),
    ROLLBACK_COMPLETED: ("release_id", "use_ids", "affected_case_refs"),
    ADVERSE_EVENT_REPORTED: (
        "adverse_id", "release_id", "use_id", "case_ref", "severity", "summary",
    ),
    RECALL_DECIDED: ("release_id", "use_ids", "reason"),
    USE_SUPERSEDED: ("release_id", "use_id", "superseded_by_release", "reason"),
}

# 患者身份字段黑名单：出现在任意嵌套层级都拒绝写入。
# 病例只能用去标识的 case_ref；患者数据不得离开原授权目的。
PHI_KEYS = frozenset(
    {
        "patient_name", "patient_id", "patient_id_card", "id_card",
        "phone", "mobile", "insurance_id", "ssn", "passport",
        "home_address", "contact_name", "emergency_contact",
    }
)


class EventContractError(ValueError):
    """事件不符合信封或负载合同。"""


def _scan_phi(payload, path: str = "payload") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_l = str(key).lower()
            if key_l in PHI_KEYS:
                found.append(f"{path}.{key}")
            else:
                found.extend(_scan_phi(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(_scan_phi(value, f"{path}[{index}]"))
    return found


def validate_event(record: dict) -> list[str]:
    """完整校验一条事件：信封 + 事件种类 + 必填负载 + PHI 边界。"""
    problems = validate_envelope(record)
    kind = record.get("kind")
    if kind not in ALL_KINDS:
        problems.append("unknown_kind")
        return problems
    payload = record.get("payload")
    if not isinstance(payload, dict):
        problems.append("payload_missing")
        return problems
    for field in REQUIRED_PAYLOAD[kind]:
        if field not in payload:
            problems.append(f"payload_missing:{field}")
    problems.extend("phi_field:" + p for p in _scan_phi(payload))
    return problems


def require_valid(record: dict) -> None:
    problems = validate_event(record)
    if problems:
        raise EventContractError(f"event {record.get('event_id')!r}: {', '.join(problems)}")
