"""临床智能体变更守门 —— 领域类型与不变量。

设计要点：

* 一切状态变化都表达为不可变事件（见 ``src.events``），本模块只定义
  结构，不做任何写入决策。
* 治理的最小单元是 *用途*（Use）：「智能体 × 科室 × 人群 × 任务边界」。
  验证泄漏、设备协议变化、亚组退化都只阻断相关用途，不牵连其他场景。
* 一次版本发布（Release）把多个用途的变更打成一份计划，委员会可从
  事件流完整还原「证据 → 审批 → 实际暴露」。
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Kind(StrEnum):
    """领域事件类型，取值即事件信封中的 ``kind``。"""

    USE_REGISTERED = "USE_REGISTERED"
    USE_SCOPE_AMENDED = "USE_SCOPE_AMENDED"
    USE_DEACTIVATED = "USE_DEACTIVATED"
    RELEASE_PLANNED = "RELEASE_PLANNED"
    PLAN_FROZEN = "PLAN_FROZEN"
    VALIDATION_EVIDENCE_RECORDED = "VALIDATION_EVIDENCE_RECORDED"
    INDEPENDENT_APPROVAL_GRANTED = "INDEPENDENT_APPROVAL_GRANTED"
    USE_ACTIVATED = "USE_ACTIVATED"
    EXPOSURE_RECORDED = "EXPOSURE_RECORDED"
    DRIFT_SIGNAL_RAISED = "DRIFT_SIGNAL_RAISED"
    DRIFT_TRIAGED = "DRIFT_TRIAGED"
    ADVERSE_EVENT_RECORDED = "ADVERSE_EVENT_RECORDED"
    USE_SUSPENDED = "USE_SUSPENDED"
    USE_RECALLED = "USE_RECALLED"
    PURPOSE_BOUNDARY_VIOLATION = "PURPOSE_BOUNDARY_VIOLATION"


class RiskTier(StrEnum):
    STANDARD = "STANDARD"          # 常规风险
    HIGH = "HIGH"                  # 高风险：独立批准 + 最小验证样本


class ReviewMode(StrEnum):
    """人工兜底强度，从强到弱。"""

    SIGN_OFF_REQUIRED = "SIGN_OFF_REQUIRED"   # 每条建议须医生签字后方可执行
    REVIEW_BEFORE_ACTION = "REVIEW_BEFORE_ACTION"
    POST_HOC_REVIEW = "POST_HOC_REVIEW"
    MONITOR_ONLY = "MONITOR_ONLY"


class DriftCause(StrEnum):
    """线上漂移信号的分诊结论。"""

    DATA_DELAY = "DATA_DELAY"                 # 数据管道延迟，指标假象
    WORKFLOW_CHANGE = "WORKFLOW_CHANGE"       # 工作流/采集口径变化
    TRUE_DEGRADATION = "TRUE_DEGRADATION"     # 真实性能下降


class GateDecision(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"                    # 临时阻断（待分诊/补救）
    RECALLED = "RECALLED"                      # 永久召回，本版本不得重新激活
    DEACTIVATED = "DEACTIVATED"                # 正常退役


class Violation(StrEnum):
    """守门拒绝原因，便于调用方与事件审计区分。"""

    USE_NOT_FOUND = "USE_NOT_FOUND"
    RELEASE_NOT_FOUND = "RELEASE_NOT_FOUND"
    DUPLICATE_USE = "DUPLICATE_USE"
    DUPLICATE_RELEASE = "DUPLICATE_RELEASE"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    PLAN_NOT_FROZEN = "PLAN_NOT_FROZEN"
    PLAN_ALREADY_FROZEN = "PLAN_ALREADY_FROZEN"
    USE_NOT_IN_PLAN = "USE_NOT_IN_PLAN"
    USE_ALREADY_ACTIVE = "USE_ALREADY_ACTIVE"
    USE_NOT_ACTIVE = "USE_NOT_ACTIVE"
    USE_RECALLED = "USE_RECALLED"
    USE_DEACTIVATED = "USE_DEACTIVATED"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    THRESHOLD_NOT_MET = "THRESHOLD_NOT_MET"
    SUBGROUP_BELOW_THRESHOLD = "SUBGROUP_BELOW_THRESHOLD"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    DATA_LEAKAGE = "DATA_LEAKAGE"
    DEVICE_PROTOCOL_MISMATCH = "DEVICE_PROTOCOL_MISMATCH"
    TRUE_DEGRADATION = "TRUE_DEGRADATION"
    INDEPENDENT_APPROVAL_MISSING = "INDEPENDENT_APPROVAL_MISSING"
    APPROVER_NOT_INDEPENDENT = "APPROVER_NOT_INDEPENDENT"
    SIGN_OFF_RULE_MISSING = "SIGN_OFF_RULE_MISSING"
    OUTSIDE_GO_LIVE_WINDOW = "OUTSIDE_GO_LIVE_WINDOW"
    CANARY_LIMIT_EXCEEDED = "CANARY_LIMIT_EXCEEDED"
    CANARY_BUCKET_REQUIRED = "CANARY_BUCKET_REQUIRED"
    REVIEW_RESPONSIBILITY_MISSING = "REVIEW_RESPONSIBILITY_MISSING"
    SUBGROUP_EVIDENCE_MISSING = "SUBGROUP_EVIDENCE_MISSING"
    SUBGROUP_SAMPLE_INSUFFICIENT = "SUBGROUP_SAMPLE_INSUFFICIENT"
    UNKNOWN_DRIFT_CAUSE = "UNKNOWN_DRIFT_CAUSE"
    REVALIDATION_REQUIRED = "REVALIDATION_REQUIRED"
    PURPOSE_BOUNDARY_DENIED = "PURPOSE_BOUNDARY_DENIED"


class GateError(Exception):
    """所有守门拒绝统一抛出，``violations`` 给出结构化原因。"""

    def __init__(self, violations: list[Violation], context: str = ""):
        self.violations = violations
        self.context = context
        super().__init__(
            f"{context + ': ' if context else ''}{', '.join(v.value for v in violations)}"
        )


@dataclass(frozen=True)
class ModelVersion:
    provider: str
    model: str
    model_version: str
    prompt_version: str
    knowledge_base_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "knowledge_base_version": self.knowledge_base_version,
        }


@dataclass(frozen=True)
class Threshold:
    """用途级性能阈值，亚组阈值在 :class:`SubgroupSpec` 内单独声明。"""

    metric: str
    minimum: float

    def as_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "minimum": self.minimum}


@dataclass(frozen=True)
class SubgroupSpec:
    """关键亚组：声明阈值与最小样本，防止总体达标掩盖亚组退化。"""

    name: str
    threshold: Threshold
    minimum_sample: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "threshold": self.threshold.as_dict(),
            "minimum_sample": self.minimum_sample,
        }


@dataclass(frozen=True)
class ReviewResponsibility:
    """人工复核职责：哪个角色、以何种模式兜底。

    自动建议永远不替代医生签字：``SIGN_OFF_REQUIRED`` 表示执行前必须有
    医生在病历上签字，系统只允许登记签字事实，不能代为生成。
    """

    role: str
    mode: ReviewMode
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "mode": self.mode.value, "rationale": self.rationale}


@dataclass(frozen=True)
class UseScope:
    """登记的用途边界。"""

    use_id: str
    title: str
    task: str                              # 任务边界，如「胸片初筛提示」
    department: str                        # 科室，如「儿科急诊」
    population: str                        # 适用人群
    contraindications: tuple[str, ...] = ()
    device_protocols: tuple[str, ...] = ()  # 验证覆盖的设备/协议标识
    risk_tier: RiskTier = RiskTier.STANDARD
    review: ReviewResponsibility | None = None
    thresholds: tuple[Threshold, ...] = ()
    subgroups: tuple[SubgroupSpec, ...] = ()
    minimum_sample: int = 0                # 总体最小验证样本
    authorized_purposes: tuple[str, ...] = ("clinical_decision_support",)

    def as_dict(self) -> dict[str, Any]:
        return {
            "use_id": self.use_id,
            "title": self.title,
            "task": self.task,
            "department": self.department,
            "population": self.population,
            "contraindications": list(self.contraindications),
            "device_protocols": list(self.device_protocols),
            "risk_tier": self.risk_tier.value,
            "review": self.review.as_dict() if self.review else None,
            "thresholds": [t.as_dict() for t in self.thresholds],
            "subgroups": [s.as_dict() for s in self.subgroups],
            "minimum_sample": self.minimum_sample,
            "authorized_purposes": list(self.authorized_purposes),
        }


@dataclass(frozen=True)
class GoLiveWindow:
    """上线窗口：仅在 ``[open, close)`` 内允许灰度放量。"""

    opens_at: _dt.datetime
    closes_at: _dt.datetime

    def contains(self, instant: _dt.datetime) -> bool:
        return self.opens_at <= instant < self.closes_at


@dataclass(frozen=True)
class CanaryPlan:
    """灰度范围：比例上限 + 受影响病例数硬上限。"""

    max_fraction: float
    case_cap: int = 0  # 0 表示不限制病例数

    def as_dict(self) -> dict[str, Any]:
        return {"max_fraction": self.max_fraction, "case_cap": self.case_cap}


@dataclass(frozen=True)
class PlannedUse:
    use_id: str
    intended_fraction: float
    canary: CanaryPlan | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "use_id": self.use_id,
            "intended_fraction": self.intended_fraction,
            "canary": self.canary.as_dict() if self.canary else None,
        }


@dataclass(frozen=True)
class MetricResult:
    metric: str
    value: float
    sample_size: int

    def as_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "value": self.value, "sample_size": self.sample_size}
