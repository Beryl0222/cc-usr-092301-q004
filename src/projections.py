"""面向委员会与科室的查询投影。

* ``ReleaseDossier``：从一次版本发布还原完整证据链——登记、候选、证据、
  审批、窗口、灰度、实际暴露、漂移、不良事件、阻断/回滚/召回。
* ``DepartmentDirectory``：科室视角的当前许可——现在能用在哪里、
  什么情况下必须人工兜底。

患者数据只在登记目的内出现：暴露列表仅返回去标识病例号；科室目录里
没有任何病例级数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import events as ev
from .domain import ReviewMode, Stage
from .state import GovernanceState


@dataclass
class ExposureSummary:
    case_ref: str
    recommendation_ref: str
    signoff_physician_id: str
    review_confirmed: bool
    override: bool
    override_reason: str | None
    occurred_at: str


@dataclass
class DossierSection:
    """档案中的一个证据环节，带事件标识与时间，支持逐环节核对。"""

    event_id: str
    kind: str
    occurred_at: str
    actor: str | None
    payload: dict


@dataclass
class ReleaseDossier:
    release_id: str
    candidate: dict | None
    purposed_uses: dict[str, dict] = field(default_factory=dict)
    timeline: list[DossierSection] = field(default_factory=list)
    validations: dict[str, dict] = field(default_factory=dict)
    approvals: dict[str, dict] = field(default_factory=dict)
    exposures: dict[str, list[ExposureSummary]] = field(default_factory=dict)
    drift_signals: dict[str, list[dict]] = field(default_factory=dict)
    adverse: dict[str, list[dict]] = field(default_factory=dict)
    rollback: dict | None = None
    recall: dict | None = None
    blocked_uses: set[str] = field(default_factory=set)
    backup_uses: set[str] = field(default_factory=set)
    superseded: dict[str, str] = field(default_factory=dict)
    _rollback_batches: list[dict] = field(default_factory=list)

    def _merge_rollback_batches(self) -> dict:
        """把可能的多批次回滚（先停 A 用途、再停 B 用途）聚合成一份档案视图。"""
        cases = sorted({c for b in self._rollback_batches for c in b["affected_case_refs"]})
        use_ids = sorted({u for b in self._rollback_batches for u in b["use_ids"]})
        return {
            "use_ids": use_ids,
            "reason": self._rollback_batches[-1]["reason"],
            "reasons": [b["reason"] for b in self._rollback_batches],
            "affected_case_refs": cases,
            "initiated_at": self._rollback_batches[0]["initiated_at"],
            "completed_at": next(
                (b.get("completed_at") for b in reversed(self._rollback_batches) if b.get("completed_at")),
                None,
            ),
            "completed": all(b["completed"] for b in self._rollback_batches),
            "batches": list(self._rollback_batches),
        }

    @property
    def affected_cases_after_rollback(self) -> list[str]:
        """回滚后仍须保留的“曾影响病例”清单（跨批次去重合并）。"""
        if not self.rollback:
            return []
        return list(self.rollback["affected_case_refs"])

    def summary(self) -> dict:
        return {
            "release_id": self.release_id,
            "version_fingerprint": {
                "model_version": self.candidate["model_version"] if self.candidate else None,
                "prompt_version": self.candidate["prompt_version"] if self.candidate else None,
                "knowledge_base_version": self.candidate["knowledge_base_version"] if self.candidate else None,
                "dataset_version": self.candidate["dataset_version"] if self.candidate else None,
            },
            "change_summary": self.candidate["change_summary"] if self.candidate else None,
            "uses": sorted(self.purposed_uses),
            "validations": [
                {"use_id": u, **v} for u, v in sorted(self.validations.items())
            ],
            "approvals": [
                {"use_id": u, **v} for u, v in sorted(self.approvals.items())
            ],
            "exposure_counts": {u: len(items) for u, items in sorted(self.exposures.items())},
            "affected_cases_after_rollback": self.affected_cases_after_rollback,
            "blocked_uses": sorted(self.blocked_uses),
            "backup_uses": sorted(self.backup_uses),
            "superseded_by": self.superseded,
            "recalled": self.recall is not None,
        }


def build_release_dossier(state: GovernanceState, release_id: str) -> ReleaseDossier:
    """从事件流完整还原一次发布的证据、审批与实际暴露。"""
    if release_id not in state.releases:
        raise KeyError(f"发布 {release_id} 不存在")
    candidate = state.releases[release_id]
    use_ids = set(candidate["use_ids"])

    dossier = ReleaseDossier(release_id=release_id, candidate=candidate)
    for use_id in candidate["use_ids"]:
        dossier.purposed_uses[use_id] = state.purposes[use_id]

    for record in state_events(state, release_id, use_ids):
        kind = record["kind"]
        p = record.get("payload", {})
        section = DossierSection(
            event_id=record["event_id"],
            kind=kind,
            occurred_at=record["occurred_at"],
            actor=record.get("actor"),
            payload=p,
        )
        dossier.timeline.append(section)

        if kind == ev.VALIDATION_SUBMITTED:
            dossier.validations[p["use_id"]] = {
                "cohort_id": p["cohort_id"],
                "dataset_version": p["dataset_version"],
                "dataset_purpose": p["dataset_purpose"],
                "sample_size": p["sample_size"],
                "overall_metrics": p["overall_metrics"],
                "subgroup_results": p["subgroup_results"],
                "leakage_screen_clear": p["leakage_screen_clear"],
                "device_protocol": p["device_protocol"],
                "submitted_by": p["submitted_by"],
            }
        elif kind == ev.VALIDATION_PASSED:
            dossier.validations.setdefault(p["use_id"], {})["gate"] = "PASSED"
        elif kind == ev.VALIDATION_FAILED:
            dossier.validations.setdefault(p["use_id"], {})["gate"] = "FAILED"
            dossier.validations[p["use_id"]]["failure_reasons"] = p["reasons"]
        elif kind == ev.COMMITTEE_DECISION:
            dossier.approvals[p["use_id"]] = {
                "decision": p["decision"],
                "approver_id": p["approver_id"],
                "independent": p["independent"],
                "rationale": p["rationale"],
            }
        elif kind == ev.EXPOSURE_RECORDED:
            dossier.exposures.setdefault(p["use_id"], []).append(
                ExposureSummary(
                    case_ref=p["case_ref"],
                    recommendation_ref=p["recommendation_ref"],
                    signoff_physician_id=p["signoff_physician_id"],
                    review_confirmed=p["review_confirmed"],
                    override=bool(p.get("override_physician_id")),
                    override_reason=p.get("override_reason"),
                    occurred_at=record["occurred_at"],
                )
            )
        elif kind == ev.DRIFT_SIGNAL_RAISED:
            dossier.drift_signals.setdefault(p["use_id"], []).append(
                {
                    "signal_id": p["signal_id"],
                    "scope": p["scope"],
                    "metric": p["metric"],
                    "observed": p["observed"],
                    "expected": p["expected"],
                    "category": None,
                }
            )
        elif kind == ev.DRIFT_CLASSIFIED:
            for item in dossier.drift_signals.get(p["use_id"], []):
                if item["signal_id"] == p["signal_id"]:
                    item["category"] = p["category"]
                    item["classified_by"] = record["actor"]
        elif kind == ev.USE_BLOCKED:
            dossier.blocked_uses.add(p["use_id"])
        elif kind == ev.HUMAN_BACKUP_ENFORCED:
            dossier.backup_uses.add(p["use_id"])
        elif kind == ev.ROLLBACK_INITIATED:
            dossier._rollback_batches.append(
                {
                    "use_ids": list(p["use_ids"]),
                    "reason": p["reason"],
                    "affected_case_refs": list(p["affected_case_refs"]),
                    "initiated_at": record["occurred_at"],
                    "completed": False,
                }
            )
            dossier.rollback = dossier._merge_rollback_batches()
        elif kind == ev.ROLLBACK_COMPLETED:
            batch = next(
                (
                    b
                    for b in reversed(dossier._rollback_batches)
                    if set(b["use_ids"]) == set(p["use_ids"]) and not b["completed"]
                ),
                None,
            )
            if batch is None:
                batch = {
                    "use_ids": list(p["use_ids"]),
                    "reason": None,
                    "affected_case_refs": list(p["affected_case_refs"]),
                    "initiated_at": None,
                }
                dossier._rollback_batches.append(batch)
            batch["completed"] = True
            batch["completed_at"] = record["occurred_at"]
            batch["affected_case_refs"] = list(p["affected_case_refs"])
            dossier.rollback = dossier._merge_rollback_batches()
        elif kind == ev.ADVERSE_EVENT_REPORTED:
            dossier.adverse.setdefault(p["use_id"], []).append(
                {
                    "adverse_id": p["adverse_id"],
                    "case_ref": p["case_ref"],
                    "severity": p["severity"],
                    "summary": p["summary"],
                    "occurred_at": record["occurred_at"],
                }
            )
        elif kind == ev.RECALL_DECIDED:
            dossier.recall = {
                "use_ids": list(p["use_ids"]),
                "reason": p["reason"],
                "decided_at": record["occurred_at"],
            }
        elif kind == ev.USE_SUPERSEDED:
            dossier.superseded[p["use_id"]] = p["superseded_by_release"]

    return dossier


def state_events(state: GovernanceState, release_id: str, use_ids: set[str]):
    """按发生顺序产出与一次发布相关的事件（含该发布覆盖用途的登记事件）。"""
    for record in state_raw_events(state):
        kind = record["kind"]
        p = record.get("payload", {})
        if kind == ev.PURPOSE_REGISTERED and p["use_id"] in use_ids:
            yield record
        elif "release_id" in p and p["release_id"] == release_id:
            yield record


def state_raw_events(state: GovernanceState):
    # 状态折叠时保留原始事件流，供档案逐环节还原。
    return getattr(state, "_raw", [])


@dataclass
class DirectoryEntry:
    department: str
    use_id: str
    agent: str
    intended_use: str
    population: str
    device_protocol: str
    release_id: str
    stage: str
    risk_class: str
    review_mode: str
    reviewer_role: str
    physician_signoff_required: bool
    canary_scope: dict | None
    backup_reason: str | None

    def to_dict(self) -> dict:
        return {
            "department": self.department,
            "use_id": self.use_id,
            "agent": self.agent,
            "intended_use": self.intended_use,
            "population": self.population,
            "device_protocol": self.device_protocol,
            "release_id": self.release_id,
            "stage": self.stage,
            "risk_class": self.risk_class,
            "review_mode": self.review_mode,
            "reviewer_role": self.reviewer_role,
            "physician_signoff_required": self.physician_signoff_required,
            "canary_scope": self.canary_scope,
            "backup_reason": self.backup_reason,
            "human_backup_required": self.stage == str(Stage.HUMAN_BACKUP)
            or self.review_mode == str(ReviewMode.REQUIRED),
        }


class DepartmentDirectory:
    """科室视角：当前允许使用的智能体用途，以及何时必须人工兜底。

    只呈现“当前有效”的结论：被阻断、回滚、召回的用途不出现在许可中；
    灰度中的用途标明仅限灰度范围。目录不含任何病例级数据。
    """

    def __init__(self, state: GovernanceState):
        # 先收集所有在线条目（已阻断/回滚/召回的不入目录）。
        online: dict[str, list[tuple[str, DirectoryEntry]]] = {}
        for (release_id, use_id), ru in state.release_uses.items():
            purpose = state.purposes[use_id]
            scope = purpose["scope"]
            if ru.stage not in (Stage.CANARY, Stage.ACTIVE, Stage.HUMAN_BACKUP):
                continue
            if state.is_recalled(release_id, use_id) or state.is_rolled_back(release_id, use_id):
                continue
            rollout = state.rollouts.get(release_id)
            canary = None
            if ru.stage == Stage.CANARY and rollout is not None:
                canary_scope = state.canary_scope_for(release_id, use_id)
                canary = canary_scope.to_dict() if canary_scope else None
            entry = DirectoryEntry(
                department=scope.department,
                use_id=use_id,
                agent=purpose["agent"],
                intended_use=purpose["intended_use"],
                population=scope.population,
                device_protocol=scope.device_protocol,
                release_id=release_id,
                stage=str(ru.stage),
                risk_class=purpose["risk_class"],
                review_mode=purpose["review_mode"],
                reviewer_role=purpose["reviewer_role"],
                physician_signoff_required=purpose.get("physician_signoff_required", True),
                canary_scope=canary,
                backup_reason=ru.backup_reason,
            )
            online.setdefault(use_id, []).append((release_id, entry))

        # 每个用途只呈现“当前有效”的版本：
        # 按该用途的发布链取最新在线版本；若最新版已全量，则旧版本被取代；
        # 若最新版仅在灰度/兜底，则保留更早的全量版本承担常规范围。
        self._entries: dict[str, list[DirectoryEntry]] = {}
        for use_id, items in online.items():
            chain = state.use_releases.get(use_id, [])
            ordered = sorted(items, key=lambda it: chain.index(it[0]))
            latest_release, latest_entry = ordered[-1]
            chosen = [latest_entry]
            if latest_entry.stage != str(Stage.ACTIVE):
                older_active = [
                    entry
                    for release_id, entry in ordered[:-1]
                    if entry.stage == str(Stage.ACTIVE)
                ]
                chosen.extend(older_active[-1:])
            for entry in chosen:
                self._entries.setdefault(entry.department, []).append(entry)

    def for_department(self, department: str) -> list[dict]:
        return [e.to_dict() for e in self._entries.get(department, [])]

    def all_departments(self) -> dict[str, list[dict]]:
        return {dept: [e.to_dict() for e in entries] for dept, entries in sorted(self._entries.items())}

    def find(self, use_id: str, department: str | None = None) -> DirectoryEntry | None:
        departments = [department] if department else list(self._entries)
        for dept in departments:
            for entry in self._entries.get(dept, []):
                if entry.use_id == use_id:
                    return entry
        return None

