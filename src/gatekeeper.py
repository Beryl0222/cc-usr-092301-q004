"""变更守门服务：登记、验证、批准、上线、暴露与召回的全部判定。

所有写操作都以追加事件落账；拒绝时抛出 :class:`GateError`，必要时也会
先落账拒绝尝试（例如目的越权），保证审计能看到「被挡住的事」。

阻断半径规则：泄漏、设备协议不匹配、亚组退化、真实漂移与不良事件都以
``use_id`` 为作用域，只挂起/召回该用途；同发布或同科室的其他用途不受
影响。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Iterable

from .domain import (
    DriftCause,
    GateDecision,
    GateError,
    GoLiveWindow,
    Kind,
    MetricResult,
    ModelVersion,
    PlannedUse,
    ReviewMode,
    RiskTier,
    UseScope,
    Violation,
)
from .events import Event, EventStore, load


class Gatekeeper:
    def __init__(self, store: EventStore):
        self.store = store
        self._refresh()

    # ------------------------------------------------------------------ 内部

    def _refresh(self) -> None:
        self.state = load(self.store.all())

    def _use(self, use_id: str) -> dict[str, Any]:
        use = self.state["uses"].get(use_id)
        if use is None:
            raise GateError([Violation.USE_NOT_FOUND], f"用途 {use_id}")
        return use

    def _release(self, release_id: str) -> dict[str, Any]:
        release = self.state["releases"].get(release_id)
        if release is None:
            raise GateError([Violation.RELEASE_NOT_FOUND], f"发布 {release_id}")
        return release

    @staticmethod
    def _fail(violations: Iterable[Violation], context: str) -> None:
        raise GateError(list(violations), context)

    # ----------------------------------------------------------- 用途边界登记

    def register_use(self, scope: UseScope) -> Event:
        if scope.use_id in self.state["uses"]:
            raise GateError([Violation.DUPLICATE_USE], f"用途 {scope.use_id}")
        event = self.store.append(Kind.USE_REGISTERED, scope.as_dict())
        self._refresh()
        return event

    def deactivate_use(self, use_id: str, *, by: str, reason: str) -> Event:
        """用途退役（非惩罚性）；历史暴露与卷宗保持可查。"""
        self._use(use_id)
        event = self.store.append(
            Kind.USE_DEACTIVATED, {"use_id": use_id, "by": by, "reason": reason}
        )
        self._refresh()
        return event

    def amend_use_scope(self, scope: UseScope, *, by: str, reason: str) -> Event:
        """修订用途边界（如设备协议升级、人群描述更新）。

        仅在用途未在线时允许；修订后须在新发布中按新边界重新验证。
        召回用途不得修订后复用，必须作为新用途重新登记。
        """
        use = self._use(scope.use_id)
        if use["status"] in ("ACTIVE", "RECALLED", "DEACTIVATED"):
            code = {
                "ACTIVE": Violation.USE_ALREADY_ACTIVE,
                "RECALLED": Violation.USE_RECALLED,
                "DEACTIVATED": Violation.USE_DEACTIVATED,
            }[use["status"]]
            self._fail([code], f"用途 {scope.use_id} 当前状态 {use['status']}，不得修订边界")
        event = self.store.append(
            Kind.USE_SCOPE_AMENDED,
            {"use_id": scope.use_id, "by": by, "reason": reason, "scope": scope.as_dict()},
        )
        self._refresh()
        return event

    # ------------------------------------------------------------- 版本发布计划

    def plan_release(
        self,
        release_id: str,
        *,
        model: ModelVersion,
        uses: list[PlannedUse],
        window: GoLiveWindow,
        planned_by: str,
        notes: str = "",
    ) -> Event:
        if release_id in self.state["releases"]:
            raise GateError([Violation.DUPLICATE_RELEASE], f"发布 {release_id}")
        if not uses:
            raise GateError([Violation.USE_NOT_IN_PLAN], "发布计划至少包含一个用途")
        for planned in uses:
            self._use(planned.use_id)
        event = self.store.append(
            Kind.RELEASE_PLANNED,
            {
                "release_id": release_id,
                "model": model.as_dict(),
                "uses": [u.as_dict() for u in uses],
                "window": {"opens_at": window.opens_at, "closes_at": window.closes_at},
                "planned_by": planned_by,
                "notes": notes,
            },
        )
        self._refresh()
        return event

    def freeze_plan(self, release_id: str, *, by: str) -> Event:
        """冻结计划：此后模型/提示词/知识库版本与用途范围不可再悄悄改动。"""
        release = self._release(release_id)
        if release["frozen"]:
            raise GateError([Violation.PLAN_ALREADY_FROZEN], f"发布 {release_id}")
        event = self.store.append(
            Kind.PLAN_FROZEN, {"release_id": release_id, "by": by}
        )
        self._refresh()
        return event

    # ----------------------------------------------------------------- 验证证据

    def record_validation(
        self,
        release_id: str,
        use_id: str,
        *,
        cohort_id: str,
        overall: list[MetricResult],
        sample_size: int,
        subgroups: dict[str, list[MetricResult]] | None = None,
        data_leakage_found: bool = False,
        device_protocols_verified: Iterable[str] = (),
        evaluator: str,
        notes: str = "",
    ) -> Event:
        release = self._release(release_id)
        if not release["frozen"]:
            raise GateError([Violation.PLAN_NOT_FROZEN], f"发布 {release_id}")
        if use_id not in release["planned"]:
            raise GateError([Violation.USE_NOT_IN_PLAN], f"用途 {use_id}")
        event = self.store.append(
            Kind.VALIDATION_EVIDENCE_RECORDED,
            {
                "release_id": release_id,
                "use_id": use_id,
                "cohort_id": cohort_id,
                "sample_size": sample_size,
                "overall": [m.as_dict() for m in overall],
                "subgroups": {
                    name: [m.as_dict() for m in metrics]
                    for name, metrics in (subgroups or {}).items()
                },
                "data_leakage_found": data_leakage_found,
                "device_protocols_verified": list(device_protocols_verified),
                "evaluator": evaluator,
                "notes": notes,
            },
        )
        self._refresh()
        return event

    # ------------------------------------------------------------- 高风险独立批准

    def grant_independent_approval(
        self,
        release_id: str,
        use_id: str,
        *,
        approver_id: str,
        approver_role: str,
        independence_declared: bool,
        conditions: Iterable[str] = (),
        notes: str = "",
    ) -> Event:
        release = self._release(release_id)
        if not release["frozen"]:
            raise GateError([Violation.PLAN_NOT_FROZEN], f"发布 {release_id}")
        if use_id not in release["planned"]:
            raise GateError([Violation.USE_NOT_IN_PLAN], f"用途 {use_id}")
        event = self.store.append(
            Kind.INDEPENDENT_APPROVAL_GRANTED,
            {
                "release_id": release_id,
                "use_id": use_id,
                "approver_id": approver_id,
                "approver_role": approver_role,
                "independence_declared": independence_declared,
                "conditions": list(conditions),
                "notes": notes,
            },
        )
        self._refresh()
        return event

    # ------------------------------------------------------------- 上线激活闸门

    def activate(
        self,
        release_id: str,
        use_id: str,
        *,
        by: str,
        at: _dt.datetime | None = None,
    ) -> Event:
        instant = at or self.store.now()
        use = self._use(use_id)
        release = self._release(release_id)

        violations: list[Violation] = []
        if not release["frozen"]:
            violations.append(Violation.PLAN_NOT_FROZEN)
        if use_id not in release["planned"]:
            violations.append(Violation.USE_NOT_IN_PLAN)
        status = use["status"]
        if status == "RECALLED":
            violations.append(Violation.USE_RECALLED)
        elif status == "ACTIVE":
            violations.append(Violation.USE_ALREADY_ACTIVE)
        elif status == "DEACTIVATED":
            violations.append(Violation.USE_DEACTIVATED)
        elif status == "SUSPENDED":
            # 挂起后不能凭旧发布直接恢复：必须在新发布中重新验证。
            if release["planned_version"] <= use.get("suspended_at_version", 0):
                violations.append(Violation.REVALIDATION_REQUIRED)

        evidence = release["evidence"].get(use_id, {})
        violations += _evaluate_evidence(use, evidence)

        if use["risk_tier"] == RiskTier.HIGH.value:
            approval = release["approvals"].get(use_id)
            if approval is None:
                violations.append(Violation.INDEPENDENT_APPROVAL_MISSING)
            elif not approval["independence_declared"]:
                violations.append(Violation.APPROVER_NOT_INDEPENDENT)

        if use["review"] is None:
            violations.append(Violation.REVIEW_RESPONSIBILITY_MISSING)

        window = release["window"]
        if not (window["opens_at"] <= instant < window["closes_at"]):
            violations.append(Violation.OUTSIDE_GO_LIVE_WINDOW)

        planned = release["planned"].get(use_id)
        if planned and planned["canary"]:
            cap = planned["canary"]["max_fraction"]
            if planned["intended_fraction"] > cap:
                violations.append(Violation.CANARY_LIMIT_EXCEEDED)

        if violations:
            self._fail(violations, f"激活被拒：{use_id} @ {release_id}")

        event = self.store.append(
            Kind.USE_ACTIVATED,
            {
                "release_id": release_id,
                "use_id": use_id,
                "by": by,
                "at": instant,
                "model": release["model"],
                "canary": planned["canary"],
                "window": release["window"],
            },
        )
        self._refresh()
        return event

    # ------------------------------------------------------------- 线上暴露登记

    def record_exposure(
        self,
        use_id: str,
        *,
        case_ref: str,
        physician_id: str,
        purpose: str,
        signed_off: bool,
        at: _dt.datetime | None = None,
        fraction_bucket: float | None = None,
        notes: str = "",
    ) -> Event:
        """登记一次「自动建议曾影响某病例」。

        任何自动建议都不替代医生签字：``signed_off`` 只能由调用方携带
        医生签字事实传入，守门服务绝不代为置真。回滚/召回后这些记录
        仍然保留，用于回溯受影响病例。
        """
        instant = at or self.store.now()
        use = self._use(use_id)

        if purpose not in (use["authorized_purposes"] or []):
            # 越权尝试本身也要留痕，但拒绝暴露。
            self.store.append(
                Kind.PURPOSE_BOUNDARY_VIOLATION,
                {
                    "use_id": use_id,
                    "case_ref": case_ref,
                    "attempted_purpose": purpose,
                    "authorized_purposes": use["authorized_purposes"],
                    "at": instant,
                },
            )
            self._refresh()
            raise GateError(
                [Violation.PURPOSE_BOUNDARY_DENIED],
                f"用途 {use_id} 未授权目的 {purpose}",
            )

        status = use["status"]
        if status == "RECALLED":
            raise GateError([Violation.USE_RECALLED], f"用途 {use_id}")
        if status != "ACTIVE":
            raise GateError([Violation.USE_NOT_ACTIVE], f"用途 {use_id}（{status}）")

        review = use["review"]
        if review and review["mode"] == ReviewMode.SIGN_OFF_REQUIRED.value and not signed_off:
            raise GateError(
                [Violation.SIGN_OFF_RULE_MISSING],
                f"用途 {use_id} / 病例 {case_ref} 缺少医生签字",
            )

        planned_canary = None
        active_release = self.state["releases"][use["active_release_id"]]
        planned = active_release["planned"].get(use_id)
        if planned and planned["canary"]:
            planned_canary = planned["canary"]
            max_fraction = planned_canary["max_fraction"]
            if fraction_bucket is None:
                raise GateError(
                    [Violation.CANARY_BUCKET_REQUIRED],
                    f"用途 {use_id} 处于灰度中，病例 {case_ref} 必须携带分流比例",
                )
            if fraction_bucket > max_fraction:
                raise GateError(
                    [Violation.CANARY_LIMIT_EXCEEDED],
                    f"用途 {use_id} 病例 {case_ref} 落入 {fraction_bucket:.0%}，"
                    f"超出灰度上限 {max_fraction:.0%}",
                )
            cap = planned_canary["case_cap"]
            # 病例上限按当前发布计数；重新验证激活后灰度重新计数，
            # 历史暴露仍完整保留在事件流中。
            current_count = sum(
                1
                for event in self.store.exposure_events(use_id)
                if event.payload["release_id"] == use["active_release_id"]
            )
            if cap and current_count >= cap:
                raise GateError(
                    [Violation.CANARY_LIMIT_EXCEEDED],
                    f"用途 {use_id} 当前灰度病例上限 {cap} 已用尽",
                )

        event = self.store.append(
            Kind.EXPOSURE_RECORDED,
            {
                "use_id": use_id,
                "release_id": use["active_release_id"],
                "case_ref": case_ref,
                "physician_id": physician_id,
                "purpose": purpose,
                "signed_off": signed_off,
                "review_mode": review["mode"] if review else None,
                "at": instant,
                "fraction_bucket": fraction_bucket,
                "model": active_release["model"],
                "notes": notes,
            },
        )
        self._refresh()
        return event

    # ------------------------------------------------------------- 漂移信号与分诊

    def raise_drift_signal(
        self,
        signal_id: str,
        use_id: str,
        *,
        metric: str,
        observed: float,
        expected: float,
        observation_window: str,
        raised_by: str,
        at: _dt.datetime | None = None,
        notes: str = "",
    ) -> Event:
        self._use(use_id)
        if signal_id in self.state["signals"]:
            raise GateError([Violation.DUPLICATE_SIGNAL], f"漂移信号 {signal_id}")
        event = self.store.append(
            Kind.DRIFT_SIGNAL_RAISED,
            {
                "signal_id": signal_id,
                "use_id": use_id,
                "metric": metric,
                "observed": observed,
                "expected": expected,
                "observation_window": observation_window,
                "raised_by": raised_by,
                "at": at or self.store.now(),
                "notes": notes,
            },
        )
        self._refresh()
        return event

    def triage_drift(
        self,
        signal_id: str,
        *,
        cause: DriftCause,
        triaged_by: str,
        affected_use_ids: Iterable[str] = (),
        resolution: str = "",
        evidence_ref: str = "",
    ) -> list[Event]:
        """分诊线上漂移：数据延迟 / 工作流变化不阻断；真实退化才阻断。

        阻断只落在 ``affected_use_ids`` 且当前在线的用途上，其余用途
        继续运行。
        """
        signal = self.state["signals"].get(signal_id)
        if signal is None:
            raise GateError([Violation.USE_NOT_FOUND], f"漂移信号 {signal_id}")
        events: list[Event] = [
            self.store.append(
                Kind.DRIFT_TRIAGED,
                {
                    "signal_id": signal_id,
                    "use_id": signal["use_id"],
                    "cause": cause.value,
                    "triaged_by": triaged_by,
                    "affected_use_ids": list(affected_use_ids),
                    "resolution": resolution,
                    "evidence_ref": evidence_ref,
                },
            )
        ]
        if cause == DriftCause.TRUE_DEGRADATION:
            for affected in affected_use_ids:
                use = self._use(affected)
                if use["status"] == "ACTIVE":
                    events.append(self._suspend(affected, Violation.TRUE_DEGRADATION, triaged_by,
                                                evidence_ref=evidence_ref,
                                                notes=f"漂移信号 {signal_id} 分诊确认真实退化"))
        self._refresh()
        return events

    # ------------------------------------------------------- 主动挂起 / 召回 / 不良事件

    def suspend_use(
        self,
        use_id: str,
        *,
        reason: Violation,
        by: str,
        evidence_ref: str = "",
        notes: str = "",
    ) -> Event:
        return self._suspend(use_id, reason, by, evidence_ref=evidence_ref, notes=notes, refresh=True)

    def _suspend(
        self,
        use_id: str,
        reason: Violation,
        by: str,
        *,
        evidence_ref: str = "",
        notes: str = "",
        refresh: bool = False,
    ) -> Event:
        use = self._use(use_id)
        if use["status"] == "RECALLED":
            raise GateError([Violation.USE_RECALLED], f"用途 {use_id}")
        if use["status"] != "ACTIVE":
            raise GateError([Violation.USE_NOT_ACTIVE], f"用途 {use_id}（{use['status']}）")
        event = self.store.append(
            Kind.USE_SUSPENDED,
            {
                "use_id": use_id,
                "release_id": use["active_release_id"],
                "reason": reason.value,
                "by": by,
                "evidence_ref": evidence_ref,
                "notes": notes,
            },
        )
        if refresh:
            self._refresh()
        return event

    def recall_use(
        self,
        use_id: str,
        *,
        reason: Violation,
        by: str,
        evidence_ref: str = "",
        committee_decision_ref: str,
        notes: str = "",
    ) -> Event:
        """委员会召回决定：该用途永久下线，重新引入须按新用途重新登记验证。"""
        use = self._use(use_id)
        if use["status"] == "RECALLED":
            raise GateError([Violation.USE_RECALLED], f"用途 {use_id}")
        event = self.store.append(
            Kind.USE_RECALLED,
            {
                "use_id": use_id,
                "release_id": use["active_release_id"],
                "reason": reason.value,
                "by": by,
                "evidence_ref": evidence_ref,
                "committee_decision_ref": committee_decision_ref,
                "notes": notes,
            },
        )
        self._refresh()
        return event

    def record_adverse_event(
        self,
        event_ref: str,
        use_id: str,
        *,
        case_ref: str,
        severity: str,
        description: str,
        reported_by: str,
        at: _dt.datetime | None = None,
    ) -> list[Event]:
        """登记不良事件；哨事件自动先行挂起，等待委员会召回/恢复决定。"""
        self._use(use_id)
        events = [
            self.store.append(
                Kind.ADVERSE_EVENT_RECORDED,
                {
                    "event_ref": event_ref,
                    "use_id": use_id,
                    "case_ref": case_ref,
                    "severity": severity,
                    "description": description,
                    "reported_by": reported_by,
                    "at": at or self.store.now(),
                },
            )
        ]
        use = self._use(use_id)
        if severity == "SENTINEL" and use["status"] == "ACTIVE":
            events.append(
                self._suspend(use_id, Violation.TRUE_DEGRADATION, reported_by,
                              evidence_ref=event_ref, notes="不良事件哨事件触发预防性挂起")
            )
        self._refresh()
        return events

    # ------------------------------------------------------------------ 查询视图

    def status(self, use_id: str) -> GateDecision:
        return GateDecision(self._use(use_id)["status"])

    def release_dossier(self, release_id: str) -> dict[str, Any]:
        """委员会卷宗：从一次版本发布还原计划、证据、审批与实际暴露。"""
        release = self._release(release_id)
        per_use: dict[str, Any] = {}
        for use_id, planned in release["planned"].items():
            scope = self.state["uses"].get(use_id)
            evidence = release["evidence"].get(use_id, {})
            exposures = [
                e.payload
                for e in self.store.exposure_events(use_id)
                if e.payload["release_id"] == release_id
            ]
            per_use[use_id] = {
                "scope": {
                    "title": scope["title"],
                    "task": scope["task"],
                    "department": scope["department"],
                    "population": scope["population"],
                    "risk_tier": scope["risk_tier"],
                    "thresholds": scope["thresholds"],
                    "subgroups": scope["subgroups"],
                    "minimum_sample": scope["minimum_sample"],
                    "review": scope["review"],
                    "device_protocols": scope["device_protocols"],
                },
                "planned": planned,
                "evidence": evidence or None,
                "independent_approval": release["approvals"].get(use_id),
                "activation": release["activations"].get(use_id),
                "current_status": scope["status"],
                "block_history": scope["block_history"],
                "exposures": exposures,
                "exposed_cases": sorted({e["case_ref"] for e in exposures}),
                "exposure_count": len(exposures),
            }
        return {
            "release_id": release_id,
            "model": release["model"],
            "window": release["window"],
            "frozen": release["frozen"],
            "planned_by": release["planned_by"],
            "uses": per_use,
        }

    def department_guide(self, department: str) -> dict[str, Any]:
        """科室视角：当前允许用在哪里、何时必须人工兜底。"""
        allowed, suspended, unavailable = [], [], []
        for use in self.state["uses"].values():
            if use["department"] != department:
                continue
            entry = {
                "use_id": use["use_id"],
                "title": use["title"],
                "task": use["task"],
                "population": use["population"],
                "contraindications": use["contraindications"],
                "review": use["review"],
                "human_fallback_required": _fallback_required(use),
            }
            if use["status"] == "ACTIVE":
                release = self.state["releases"][use["active_release_id"]]
                entry.update(
                    {
                        "release_id": use["active_release_id"],
                        "model": release["model"],
                        "canary": use["canary"],
                        "window": release["window"],
                    }
                )
                allowed.append(entry)
            elif use["status"] in ("SUSPENDED", "RECALLED"):
                entry["reason"] = use["block_history"][-1]["reason"] if use["block_history"] else use["status"]
                suspended.append(entry)
            else:
                unavailable.append(entry)
        return {
            "department": department,
            "allowed_now": allowed,
            "suspended_or_recalled": suspended,
            "not_yet_activated": unavailable,
        }

    def affected_cases(
        self, *, use_id: str | None = None, release_id: str | None = None
    ) -> list[dict[str, Any]]:
        """回滚后仍可查：某用途/某版本曾影响过哪些病例。"""
        out = []
        for event in self.store.exposure_events(use_id):
            if release_id and event.payload["release_id"] != release_id:
                continue
            out.append(event.payload)
        return out


def _fallback_required(use: dict[str, Any]) -> bool:
    review = use["review"]
    if review is None:
        return True
    return review["mode"] in (
        ReviewMode.SIGN_OFF_REQUIRED.value,
        ReviewMode.REVIEW_BEFORE_ACTION.value,
    )


def _evaluate_evidence(use: dict[str, Any], evidence: dict[str, Any]) -> list[Violation]:
    violations: list[Violation] = []
    if not evidence:
        violations.append(Violation.EVIDENCE_MISSING)
        violations.extend(Violation.SUBGROUP_EVIDENCE_MISSING for _ in use["subgroups"])
        return violations

    # 验证数据泄漏：只阻断本用途，其他用途由各自的证据独立判定。
    if evidence.get("data_leakage_found"):
        violations.append(Violation.DATA_LEAKAGE)

    verified = set(evidence.get("device_protocols_verified") or [])
    required_protocols = set(use["device_protocols"] or [])
    if required_protocols and not required_protocols.issubset(verified):
        violations.append(Violation.DEVICE_PROTOCOL_MISMATCH)

    if evidence["sample_size"] < use["minimum_sample"]:
        violations.append(Violation.INSUFFICIENT_SAMPLE)

    overall_metrics = {m["metric"]: m["value"] for m in evidence.get("overall", [])}
    for threshold in use["thresholds"]:
        value = overall_metrics.get(threshold["metric"])
        if value is None:
            violations.append(Violation.EVIDENCE_MISSING)
        elif value < threshold["minimum"]:
            violations.append(Violation.THRESHOLD_NOT_MET)

    subgroup_results = evidence.get("subgroups", {})
    for spec in use["subgroups"]:
        results = subgroup_results.get(spec["name"])
        if not results:
            violations.append(Violation.SUBGROUP_EVIDENCE_MISSING)
            continue
        metrics = {m["metric"]: m for m in results}
        sample = min((m["sample_size"] for m in metrics.values()), default=0)
        if sample < spec["minimum_sample"]:
            violations.append(Violation.SUBGROUP_SAMPLE_INSUFFICIENT)
        observed = metrics.get(spec["threshold"]["metric"])
        if observed is None:
            violations.append(Violation.SUBGROUP_EVIDENCE_MISSING)
        elif observed["value"] < spec["threshold"]["minimum"]:
            violations.append(Violation.SUBGROUP_BELOW_THRESHOLD)

    return violations
