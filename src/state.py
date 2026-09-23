"""读模型：重放仅追加事件，还原用途与发布的当前状态。

投影本身不做任何业务裁决——规则全部在 gatekeeper 中；这里只负责把事件流
折叠成可查询状态。由于状态完全由事件派生，任何一次发布都可以被完整还原。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import events as ev
from .domain import DriftCategory, Scope, Stage


@dataclass
class ValidationEvidence:
    cohort_id: str
    dataset_version: str
    dataset_purpose: str
    sample_size: int
    overall_metrics: dict
    subgroup_results: dict
    leakage_screen_clear: bool
    device_protocol: str
    passed: bool
    submitted_by: str | None = None
    failure_reasons: list[str] = field(default_factory=list)


@dataclass
class ReleaseUse:
    release_id: str
    use_id: str
    stage: Stage = Stage.CANDIDATE
    evidence: ValidationEvidence | None = None
    committee_decision: str | None = None
    approver_id: str | None = None
    independent: bool | None = None
    rationale: str | None = None
    block_reasons: list[str] = field(default_factory=list)
    backup_reason: str | None = None
    activated_scope: Scope | None = None
    superseded_by: str | None = None


@dataclass
class Signal:
    signal_id: str
    release_id: str
    use_id: str
    scope: Scope
    metric: str
    observed: float
    expected: float
    category: DriftCategory | None = None


@dataclass
class Exposure:
    release_id: str
    use_id: str
    scope: Scope
    case_ref: str
    recommendation_ref: str
    signoff_physician_id: str
    override_physician_id: str | None
    override_reason: str | None
    review_confirmed: bool


@dataclass
class AdverseEvent:
    adverse_id: str
    release_id: str
    use_id: str
    case_ref: str
    severity: str
    summary: str


@dataclass
class Rollback:
    release_id: str
    use_ids: list[str]
    reason: str
    affected_case_refs: list[str]
    completed: bool = False


class GovernanceState:
    """重放事件流得到的完整状态。"""

    def __init__(self, event_stream: list[dict]):
        # 原始事件流：状态可折叠，但档案需要逐环节还原证据。
        self._raw = [dict(e) for e in event_stream]
        self.purposes: dict[str, dict] = {}
        self.releases: dict[str, dict] = {}
        self.release_order: list[str] = []
        self.release_uses: dict[tuple[str, str], ReleaseUse] = {}
        # use_id -> 该用途出现过的发布（按登记顺序）
        self.use_releases: dict[str, list[str]] = {}
        self.rollouts: dict[str, dict] = {}
        self.exposures: list[Exposure] = []
        self.signals: dict[str, Signal] = {}
        self.adverse: list[AdverseEvent] = []
        self.rollbacks: dict[str, list[Rollback]] = {}
        self.recalls: dict[str, dict] = {}
        for record in event_stream:
            self._apply(record)

    # ── 折叠 ───────────────────────────────────────────────────────────────

    def _apply(self, record: dict) -> None:
        kind = record["kind"]
        p = record.get("payload", {})
        at = record["occurred_at"]

        if kind == ev.PURPOSE_REGISTERED:
            self.purposes[p["use_id"]] = {
                **p,
                "scope": Scope.from_dict(p["scope"]),
                "registered_at": at,
            }
        elif kind == ev.VERSION_CANDIDATE_REGISTERED:
            self.releases[p["release_id"]] = {**p, "registered_at": at}
            self.release_order.append(p["release_id"])
            for use_id in p["use_ids"]:
                self.release_uses[(p["release_id"], use_id)] = ReleaseUse(
                    p["release_id"], use_id
                )
                self.use_releases.setdefault(use_id, []).append(p["release_id"])
        elif kind == ev.VALIDATION_SUBMITTED:
            ru = self.release_uses[(p["release_id"], p["use_id"])]
            ru.evidence = ValidationEvidence(
                cohort_id=p["cohort_id"],
                dataset_version=p["dataset_version"],
                dataset_purpose=p["dataset_purpose"],
                sample_size=p["sample_size"],
                overall_metrics=p["overall_metrics"],
                subgroup_results=p["subgroup_results"],
                leakage_screen_clear=p["leakage_screen_clear"],
                device_protocol=p["device_protocol"],
                passed=False,
                submitted_by=p.get("submitted_by"),
            )
        elif kind == ev.VALIDATION_PASSED:
            ru = self.release_uses[(p["release_id"], p["use_id"])]
            if ru.evidence is not None:
                ru.evidence.passed = True
            ru.stage = Stage.VALIDATED
        elif kind == ev.VALIDATION_FAILED:
            ru = self.release_uses[(p["release_id"], p["use_id"])]
            if ru.evidence is not None:
                ru.evidence.passed = False
                ru.evidence.failure_reasons = list(p["reasons"])
        elif kind == ev.COMMITTEE_DECISION:
            ru = self.release_uses[(p["release_id"], p["use_id"])]
            ru.committee_decision = p["decision"]
            ru.approver_id = p["approver_id"]
            ru.independent = p["independent"]
            ru.rationale = p["rationale"]
            if p["decision"] == "APPROVE":
                ru.stage = Stage.APPROVED
        elif kind == ev.ROLLOUT_SCHEDULED:
            rollout = self.rollouts.setdefault(
                p["release_id"],
                {"use_ids": [], "windows": [], "canary_scope": None},
            )
            rollout["windows"].append(
                {
                    "use_ids": list(p["use_ids"]),
                    "window_start": p["window_start"],
                    "window_end": p["window_end"],
                    "scheduled_at": at,
                }
            )
            for use_id in p["use_ids"]:
                if use_id not in rollout["use_ids"]:
                    rollout["use_ids"].append(use_id)
                self.release_uses[(p["release_id"], use_id)].stage = Stage.SCHEDULED
            rollout["canary_scope"] = Scope.from_dict(p["canary_scope"])
        elif kind == ev.CANARY_STARTED:
            rollout = self.rollouts[p["release_id"]]
            window = next(
                (w for w in reversed(rollout["windows"]) if set(p["use_ids"]) & set(w["use_ids"])),
                rollout["windows"][-1],
            )
            window["canary_scope"] = Scope.from_dict(p["canary_scope"])
            window["canary_use_ids"] = list(p["use_ids"])
            window["canary_started_at"] = at
            # 最近一次灰度范围供暴露范围校验使用。
            rollout["canary_scope"] = Scope.from_dict(p["canary_scope"])
            rollout["canary_use_ids"] = list(p["use_ids"])
            for use_id in p["use_ids"]:
                self.release_uses[(p["release_id"], use_id)].stage = Stage.CANARY
        elif kind == ev.USE_ACTIVATED:
            ru = self.release_uses[(p["release_id"], p["use_id"])]
            ru.stage = Stage.ACTIVE
            ru.activated_scope = Scope.from_dict(p["scope"])
        elif kind == ev.EXPOSURE_RECORDED:
            self.exposures.append(
                Exposure(
                    release_id=p["release_id"],
                    use_id=p["use_id"],
                    scope=Scope.from_dict(p["scope"]),
                    case_ref=p["case_ref"],
                    recommendation_ref=p["recommendation_ref"],
                    signoff_physician_id=p["signoff_physician_id"],
                    override_physician_id=p.get("override_physician_id"),
                    override_reason=p.get("override_reason"),
                    review_confirmed=p.get("review_confirmed", False),
                )
            )
        elif kind == ev.DRIFT_SIGNAL_RAISED:
            self.signals[p["signal_id"]] = Signal(
                signal_id=p["signal_id"],
                release_id=p["release_id"],
                use_id=p["use_id"],
                scope=Scope.from_dict(p["scope"]),
                metric=p["metric"],
                observed=p["observed"],
                expected=p["expected"],
            )
        elif kind == ev.DRIFT_CLASSIFIED:
            self.signals[p["signal_id"]].category = DriftCategory(p["category"])
        elif kind == ev.USE_BLOCKED:
            ru = self.release_uses[(p["release_id"], p["use_id"])]
            ru.stage = Stage.BLOCKED
            ru.block_reasons = list(p["reasons"])
        elif kind == ev.HUMAN_BACKUP_ENFORCED:
            ru = self.release_uses[(p["release_id"], p["use_id"])]
            ru.stage = Stage.HUMAN_BACKUP
            ru.backup_reason = p["reason"]
        elif kind == ev.ROLLBACK_INITIATED:
            self.rollbacks.setdefault(p["release_id"], []).append(
                Rollback(
                    release_id=p["release_id"],
                    use_ids=list(p["use_ids"]),
                    reason=p["reason"],
                    affected_case_refs=list(p["affected_case_refs"]),
                )
            )
        elif kind == ev.ROLLBACK_COMPLETED:
            # 找到本批次的 initiated 记录并补全；同一批次的 completed 紧随其后。
            batches = self.rollbacks.setdefault(p["release_id"], [])
            batch = next(
                (
                    b
                    for b in reversed(batches)
                    if set(b.use_ids) == set(p["use_ids"]) and not b.completed
                ),
                None,
            )
            if batch is None:
                batch = Rollback(
                    p["release_id"], list(p["use_ids"]), "", list(p["affected_case_refs"])
                )
                batches.append(batch)
            batch.completed = True
            batch.affected_case_refs = list(p["affected_case_refs"])
            for use_id in p["use_ids"]:
                self.release_uses[(p["release_id"], use_id)].stage = Stage.ROLLED_BACK
        elif kind == ev.ADVERSE_EVENT_REPORTED:
            self.adverse.append(
                AdverseEvent(
                    adverse_id=p["adverse_id"],
                    release_id=p["release_id"],
                    use_id=p["use_id"],
                    case_ref=p["case_ref"],
                    severity=p["severity"],
                    summary=p["summary"],
                )
            )
        elif kind == ev.RECALL_DECIDED:
            self.recalls[p["release_id"]] = {
                "use_ids": list(p["use_ids"]),
                "reason": p["reason"],
                "decided_at": at,
            }
        elif kind == ev.USE_SUPERSEDED:
            ru = self.release_uses[(p["release_id"], p["use_id"])]
            ru.stage = Stage.SUPERSEDED
            ru.superseded_by = p["superseded_by_release"]

    # ── 查询辅助 ───────────────────────────────────────────────────────────

    def use(self, release_id: str, use_id: str) -> ReleaseUse:
        return self.release_uses[(release_id, use_id)]

    def latest_release_for(self, use_id: str) -> str | None:
        chain = self.use_releases.get(use_id)
        return chain[-1] if chain else None

    def is_recalled(self, release_id: str, use_id: str) -> bool:
        recall = self.recalls.get(release_id)
        return bool(recall and use_id in recall["use_ids"])

    def is_rolled_back(self, release_id: str, use_id: str) -> bool:
        return any(use_id in b.use_ids for b in self.rollbacks.get(release_id, []))

    def rollback_batches(self, release_id: str) -> list[Rollback]:
        return list(self.rollbacks.get(release_id, []))

    def exposures_for(self, release_id: str, use_ids: list[str] | None = None) -> list[Exposure]:
        result = self.exposures
        if use_ids is not None:
            result = [e for e in result if e.use_id in use_ids]
        return [e for e in result if e.release_id == release_id]

    def signals_for(self, release_id: str, use_id: str | None = None) -> list[Signal]:
        out = [s for s in self.signals.values() if s.release_id == release_id]
        if use_id is not None:
            out = [s for s in out if s.use_id == use_id]
        return out

    def canary_scope_for(self, release_id: str, use_id: str) -> Scope | None:
        """取某用途所属灰度批次的灰度范围；该用途未在灰度中则返回 None。"""
        rollout = self.rollouts.get(release_id)
        if rollout is None:
            return None
        for window in rollout["windows"]:
            if use_id in window.get("canary_use_ids", []):
                return window["canary_scope"]
        return None
