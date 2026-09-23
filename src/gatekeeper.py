"""智能体变更守门服务。

写侧只产出仅追加事件；所有规则（最小样本、独立批准、签字、漂移三分类、
最小阻断半径、回滚病例留存、召回）都在这里强制执行，读模型不含裁决逻辑。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from . import events as ev
from .domain import Decision, DriftCategory, ReviewMode, RiskClass, Scope, Stage
from .policy import evaluate_validation
from .state import GovernanceState
from .store import EventStore

# 高风险用途的系统级下限：登记时若低于该值，守门人直接拒绝。
HIGH_RISK_MIN_SAMPLE = 500
HIGH_RISK_MIN_SUBGROUP_SAMPLE = 100

ONLINE_STAGES = frozenset({Stage.CANARY, Stage.ACTIVE, Stage.HUMAN_BACKUP})


class GatekeeperError(ValueError):
    """命令违反守门规则。"""


class Gatekeeper:
    def __init__(
        self,
        store: EventStore,
        subject_id: str = "clinical-agent-release-governance",
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.subject_id = subject_id
        self._clock = clock

    # ── 内部工具 ───────────────────────────────────────────────────────────

    def _state(self) -> GovernanceState:
        return GovernanceState(self.store.replay(self.subject_id))

    def _emit(self, kind: str, payload: dict, actor: str, metadata: dict | None = None) -> dict:
        record = {
            "event_id": f"{kind.lower()}-{self.store.next_version(self.subject_id):05d}-{uuid4().hex[:8]}",
            "kind": kind,
            "occurred_at": self._clock().astimezone(timezone.utc).isoformat(),
            "subject_id": self.subject_id,
            "version": self.store.next_version(self.subject_id),
            "actor": actor,
            "payload": payload,
        }
        if metadata:
            record["metadata"] = metadata
        self.store.append(record)
        return record

    def _require_use(self, state: GovernanceState, release_id: str, use_id: str):
        try:
            return state.release_uses[(release_id, use_id)]
        except KeyError:
            raise GatekeeperError(f"用途 {use_id} 不属于发布 {release_id}")

    def _require_live(self, state: GovernanceState, release_id: str, use_id: str):
        ru = self._require_use(state, release_id, use_id)
        if state.is_recalled(release_id, use_id):
            raise GatekeeperError(f"发布 {release_id} 用途 {use_id} 已被召回，禁止继续暴露")
        if state.is_rolled_back(release_id, use_id) or ru.stage == Stage.ROLLED_BACK:
            raise GatekeeperError(f"发布 {release_id} 用途 {use_id} 已回滚，禁止继续暴露")
        if ru.stage == Stage.BLOCKED:
            raise GatekeeperError(f"发布 {release_id} 用途 {use_id} 已阻断，禁止继续暴露")
        return ru

    # ── 1. 用途登记 ────────────────────────────────────────────────────────

    def register_purpose(
        self,
        *,
        use_id: str,
        agent: str,
        intended_use: str,
        risk_class: RiskClass,
        scope: Scope,
        key_subgroups: list[str],
        thresholds: dict[str, float],
        review_mode: ReviewMode,
        reviewer_role: str,
        data_purpose: str,
        min_sample: int,
        min_subgroup_sample: int,
        subgroup_thresholds: dict[str, float] | None = None,
        actor: str,
    ) -> dict:
        """登记用途边界。边界一旦登记，后续版本只能在边界内被验证与放行。"""
        state = self._state()
        if use_id in state.purposes:
            raise GatekeeperError(f"用途 {use_id} 已登记，边界不可覆盖（请登记新版本用途）")
        if not intended_use.strip() or not agent.strip():
            raise GatekeeperError("智能体与用途说明不能为空")
        if not thresholds:
            raise GatekeeperError("必须登记至少一项性能阈值")
        if not key_subgroups:
            raise GatekeeperError("必须登记关键亚组（无亚组区分的临床用途不成立）")
        if not reviewer_role.strip():
            raise GatekeeperError("必须登记人工复核职责岗位")
        if not data_purpose.strip():
            raise GatekeeperError("必须登记患者数据授权目的")
        if risk_class == RiskClass.HIGH_RISK:
            if min_sample < HIGH_RISK_MIN_SAMPLE:
                raise GatekeeperError(
                    f"高风险用途最小样本不得低于 {HIGH_RISK_MIN_SAMPLE}，当前 {min_sample}"
                )
            if min_subgroup_sample < HIGH_RISK_MIN_SUBGROUP_SAMPLE:
                raise GatekeeperError(
                    f"高风险用途亚组最小样本不得低于 {HIGH_RISK_MIN_SUBGROUP_SAMPLE}"
                )
        payload = {
            "use_id": use_id,
            "agent": agent,
            "intended_use": intended_use,
            "risk_class": str(risk_class),
            "scope": scope.to_dict(),
            "key_subgroups": list(key_subgroups),
            "thresholds": dict(thresholds),
            "subgroup_thresholds": dict(subgroup_thresholds or thresholds),
            "review_mode": str(review_mode),
            "reviewer_role": reviewer_role,
            "data_purpose": data_purpose,
            "min_sample": min_sample,
            "min_subgroup_sample": min_subgroup_sample,
            # 自动建议永远不能关闭医生签字：该标志不可由调用方设置为 False。
            "physician_signoff_required": True,
        }
        return self._emit(ev.PURPOSE_REGISTERED, payload, actor)

    # ── 2. 版本候选 ────────────────────────────────────────────────────────

    def register_version_candidate(
        self,
        *,
        release_id: str,
        use_ids: list[str],
        model_version: str,
        prompt_version: str,
        knowledge_base_version: str,
        dataset_version: str,
        change_summary: str,
        actor: str,
    ) -> dict:
        """登记一次版本发布候选；四类版本指纹缺一不可（任一变化即须重新验证）。"""
        state = self._state()
        if release_id in state.releases:
            raise GatekeeperError(f"发布 {release_id} 已存在")
        if not use_ids:
            raise GatekeeperError("发布至少覆盖一个用途")
        for use_id in use_ids:
            if use_id not in state.purposes:
                raise GatekeeperError(f"用途 {use_id} 尚未登记，不能纳入发布")
        for label, value in (
            ("model_version", model_version),
            ("prompt_version", prompt_version),
            ("knowledge_base_version", knowledge_base_version),
            ("dataset_version", dataset_version),
        ):
            if not str(value).strip():
                raise GatekeeperError(f"{label} 不能为空：版本指纹缺失则无法还原证据")
        if not change_summary.strip():
            raise GatekeeperError("必须说明本次变更内容（供应商更新/提示词/知识库等）")
        payload = {
            "release_id": release_id,
            "use_ids": list(use_ids),
            "model_version": model_version,
            "prompt_version": prompt_version,
            "knowledge_base_version": knowledge_base_version,
            "dataset_version": dataset_version,
            "change_summary": change_summary,
        }
        return self._emit(ev.VERSION_CANDIDATE_REGISTERED, payload, actor)

    # ── 3. 验证闸门 ────────────────────────────────────────────────────────

    def submit_validation(
        self,
        *,
        release_id: str,
        use_id: str,
        cohort_id: str,
        dataset_version: str,
        dataset_purpose: str,
        sample_size: int,
        overall_metrics: dict[str, float],
        subgroup_results: dict[str, dict],
        leakage_screen_clear: bool,
        device_protocol: str,
        submitted_by: str,
        actor: str,
    ) -> list[str]:
        """提交验证证据并立即执行闸门；返回失败原因（空列表表示通过）。

        闸门结论只作用于当前 (release, use)——一个用途失败不影响同版本其他用途。
        """
        state = self._state()
        ru = self._require_use(state, release_id, use_id)
        if ru.stage != Stage.CANDIDATE:
            raise GatekeeperError(
                f"用途 {use_id} 当前阶段 {ru.stage}，只有候选阶段可提交验证"
            )
        payload = {
            "release_id": release_id,
            "use_id": use_id,
            "cohort_id": cohort_id,
            "dataset_version": dataset_version,
            "dataset_purpose": dataset_purpose,
            "sample_size": sample_size,
            "overall_metrics": dict(overall_metrics),
            "subgroup_results": subgroup_results,
            "leakage_screen_clear": leakage_screen_clear,
            "device_protocol": device_protocol,
            "submitted_by": submitted_by,
        }
        self._emit(ev.VALIDATION_SUBMITTED, payload, actor)

        # 用写入后的状态重放，保证裁决与落盘证据完全一致。
        state = self._state()
        ru = state.use(release_id, use_id)
        purpose = state.purposes[use_id]
        candidate = state.releases[release_id]
        reasons = evaluate_validation(purpose, candidate, ru.evidence)
        if reasons:
            self._emit(
                ev.VALIDATION_FAILED,
                {"release_id": release_id, "use_id": use_id, "reasons": reasons},
                actor,
            )
        else:
            self._emit(ev.VALIDATION_PASSED, {"release_id": release_id, "use_id": use_id}, actor)
        return reasons

    # ── 4. 委员会审批 ──────────────────────────────────────────────────────

    def committee_decide(
        self,
        *,
        release_id: str,
        use_id: str,
        decision: Decision,
        approver_id: str,
        independent: bool,
        rationale: str,
        actor: str,
    ) -> dict:
        state = self._state()
        ru = self._require_use(state, release_id, use_id)
        if not rationale.strip():
            raise GatekeeperError("审批必须写明理由")
        if not approver_id.strip():
            raise GatekeeperError("必须记录批准人")
        if decision == Decision.APPROVE:
            if ru.stage != Stage.VALIDATED:
                raise GatekeeperError(f"用途 {use_id} 未通过验证闸门，不能批准（阶段 {ru.stage}）")
            purpose = state.purposes[use_id]
            if purpose["risk_class"] == str(RiskClass.HIGH_RISK):
                if not independent:
                    raise GatekeeperError("高风险用途必须由独立批准人批准")
                submitter = ru.evidence.submitted_by if ru.evidence else None
                if submitter and approver_id == submitter:
                    raise GatekeeperError("独立批准人不得是该用途的验证提交人")
        payload = {
            "release_id": release_id,
            "use_id": use_id,
            "decision": str(decision),
            "approver_id": approver_id,
            "independent": bool(independent),
            "rationale": rationale,
        }
        return self._emit(ev.COMMITTEE_DECISION, payload, actor)

    # ── 5. 上线窗口与灰度 ──────────────────────────────────────────────────

    def schedule_rollout(
        self,
        *,
        release_id: str,
        use_ids: list[str],
        window_start: str,
        window_end: str,
        canary_scope: Scope,
        actor: str,
    ) -> dict:
        """为发布中的一组用途安排上线窗口。

        同一发布的不同用途可分批排期（如全科先行、儿科观察后再排），
        但同一用途在同一发布里只能排期一次。
        """
        state = self._state()
        if release_id not in state.releases:
            raise GatekeeperError(f"发布 {release_id} 不存在")
        from .contract import parse_timestamp

        start, end = parse_timestamp(window_start), parse_timestamp(window_end)
        if start >= end:
            raise GatekeeperError("上线窗口开始时间必须早于结束时间")
        known_scopes = {state.purposes[u]["scope"].key() for u in use_ids}
        if canary_scope.key() not in known_scopes:
            raise GatekeeperError("灰度范围必须落在已批准用途登记的科室/人群/设备范围内")
        already = set(state.rollouts.get(release_id, {}).get("use_ids", []))
        now = self._clock().astimezone(timezone.utc)

        for use_id in use_ids:
            ru = self._require_use(state, release_id, use_id)
            if use_id in already:
                # 已进入灰度或更后阶段的用途不可重排；仅“已排期但窗口过期”允许重排。
                if ru.stage != Stage.SCHEDULED:
                    raise GatekeeperError(f"用途 {use_id} 已开始放行，不可重复排期")
                prior = self._window_for(state, release_id, [use_id])
                if prior is not None and now <= parse_timestamp(prior["window_end"]):
                    raise GatekeeperError(f"用途 {use_id} 的上线窗口仍有效，不可重复排期")
            if ru.stage != Stage.APPROVED and ru.stage != Stage.SCHEDULED:
                raise GatekeeperError(f"用途 {use_id} 未获批准，不能排期（阶段 {ru.stage}）")
        payload = {
            "release_id": release_id,
            "use_ids": list(use_ids),
            "window_start": window_start,
            "window_end": window_end,
            "canary_scope": canary_scope.to_dict(),
        }
        return self._emit(ev.ROLLOUT_SCHEDULED, payload, actor)

    @staticmethod
    def _window_for(state: GovernanceState, release_id: str, use_ids: list[str]):
        rollout = state.rollouts.get(release_id)
        if rollout is None:
            return None
        wanted = set(use_ids)
        matches = [w for w in rollout["windows"] if wanted <= set(w["use_ids"])]
        return matches[-1] if matches else None

    def start_canary(self, *, release_id: str, use_ids: list[str], canary_scope: Scope, actor: str) -> dict:
        state = self._state()
        if release_id not in state.releases:
            raise GatekeeperError(f"发布 {release_id} 不存在")
        window = self._window_for(state, release_id, use_ids)
        if window is None:
            raise GatekeeperError("该组用途尚未安排上线窗口")
        from .contract import parse_timestamp

        now = self._clock().astimezone(timezone.utc)
        if now < parse_timestamp(window["window_start"]):
            raise GatekeeperError("尚未进入上线窗口，不能开始灰度")
        if now > parse_timestamp(window["window_end"]):
            raise GatekeeperError("上线窗口已关闭，须重新排期")
        for use_id in use_ids:
            ru = self._require_live(state, release_id, use_id)
            if ru.stage != Stage.SCHEDULED:
                raise GatekeeperError(f"用途 {use_id} 阶段 {ru.stage}，不能开始灰度")
        payload = {
            "release_id": release_id,
            "use_ids": list(use_ids),
            "canary_scope": canary_scope.to_dict(),
        }
        return self._emit(ev.CANARY_STARTED, payload, actor)

    def activate_use(self, *, release_id: str, use_id: str, actor: str, scope: Scope | None = None) -> dict:
        """灰度观察通过后，把单个用途从灰度推进到全量；逐用途放行。

        HUMAN_BACKUP 不能靠本命令静默升回全量——漂移兜底解除必须重新走
        验证与批准（登记新版本发布），防止指标刚恢复就拿掉人工防线。
        """
        state = self._state()
        ru = self._require_live(state, release_id, use_id)
        if ru.stage != Stage.CANARY:
            raise GatekeeperError(
                f"用途 {use_id} 阶段 {ru.stage}，不能全量上线；"
                "人工兜底的解除须通过新版本重新验证与批准"
            )
        target_scope = scope or state.purposes[use_id]["scope"]
        if target_scope.key() != state.purposes[use_id]["scope"].key():
            raise GatekeeperError("全量上线范围不得超出登记边界")
        emitted = self._emit(
            ev.USE_ACTIVATED,
            {"release_id": release_id, "use_id": use_id, "scope": target_scope.to_dict()},
            actor,
        )
        # 新版本全量即自动取代同用途的其他在线版本（仅标记退役，证据与暴露不删）。
        state = self._state()
        for older_release in state.use_releases.get(use_id, []):
            if older_release == release_id:
                continue
            older = state.release_uses.get((older_release, use_id))
            if older is not None and older.stage in ONLINE_STAGES:
                self._emit(
                    ev.USE_SUPERSEDED,
                    {
                        "release_id": older_release,
                        "use_id": use_id,
                        "superseded_by_release": release_id,
                        "reason": f"同用途更新版本 {release_id} 已全量上线",
                    },
                    actor,
                )
        return emitted

    # ── 6. 在线暴露记账：医生签字不可替代，覆盖必须给理由 ───────────────────

    def record_exposure(
        self,
        *,
        release_id: str,
        use_id: str,
        case_ref: str,
        recommendation_ref: str,
        signoff_physician_id: str,
        review_confirmed: bool,
        actor: str,
        scope: Scope | None = None,
        override_physician_id: str | None = None,
        override_reason: str | None = None,
    ) -> dict:
        state = self._state()
        ru = self._require_live(state, release_id, use_id)
        if ru.stage not in ONLINE_STAGES:
            raise GatekeeperError(f"用途 {use_id} 阶段 {ru.stage}，不在在线状态，不能产生暴露")
        if not case_ref.strip():
            raise GatekeeperError("必须记录去标识病例号 case_ref")
        if not signoff_physician_id.strip():
            raise GatekeeperError("任何自动建议都不能替代医生签字：缺少签字医生")
        purpose = state.purposes[use_id]
        effective_scope = scope or purpose["scope"]
        if ru.stage == Stage.CANARY:
            allowed = state.canary_scope_for(release_id, use_id)
            if allowed is None or effective_scope.key() != allowed.key():
                raise GatekeeperError("灰度期间只能在该用途的灰度范围内产生暴露")
        elif effective_scope.key() != purpose["scope"].key():
            raise GatekeeperError("暴露范围超出用途登记边界")
        if ru.stage == Stage.HUMAN_BACKUP and not review_confirmed:
            raise GatekeeperError(
                f"用途 {use_id} 已被升级为强制人工兜底，未确认人工复核不能记账"
            )
        if purpose["review_mode"] == str(ReviewMode.REQUIRED) and not review_confirmed:
            raise GatekeeperError(
                f"用途 {use_id} 要求逐例人工复核（{purpose['reviewer_role']}），未确认复核不能记账"
            )
        if override_physician_id and not override_reason:
            raise GatekeeperError("医生覆盖建议时必须记录覆盖理由")
        if override_reason and not override_physician_id:
            raise GatekeeperError("记录覆盖理由时必须指明覆盖医生")
        payload = {
            "release_id": release_id,
            "use_id": use_id,
            "scope": effective_scope.to_dict(),
            "case_ref": case_ref,
            "recommendation_ref": recommendation_ref,
            "signoff_physician_id": signoff_physician_id,
            "review_confirmed": bool(review_confirmed),
        }
        if override_physician_id:
            payload["override_physician_id"] = override_physician_id
            payload["override_reason"] = override_reason
        return self._emit(ev.EXPOSURE_RECORDED, payload, actor)

    # ── 7. 漂移：先登记信号，强制三分类，再决定处置 ────────────────────────

    def raise_drift_signal(
        self,
        *,
        signal_id: str,
        release_id: str,
        use_id: str,
        scope: Scope,
        metric: str,
        observed: float,
        expected: float,
        actor: str,
    ) -> dict:
        state = self._state()
        self._require_live(state, release_id, use_id)
        if signal_id in state.signals:
            raise GatekeeperError(f"漂移信号 {signal_id} 已存在")
        payload = {
            "signal_id": signal_id,
            "release_id": release_id,
            "use_id": use_id,
            "scope": scope.to_dict(),
            "metric": metric,
            "observed": observed,
            "expected": expected,
        }
        return self._emit(ev.DRIFT_SIGNAL_RAISED, payload, actor)

    def classify_drift(self, *, signal_id: str, category: DriftCategory, actor: str, note: str = "") -> dict:
        state = self._state()
        signal = state.signals.get(signal_id)
        if signal is None:
            raise GatekeeperError(f"漂移信号 {signal_id} 不存在")
        if signal.category is not None:
            raise GatekeeperError("漂移信号只能分类一次，分类结论不可修改（请新立信号）")
        payload = {
            "signal_id": signal_id,
            "release_id": signal.release_id,
            "use_id": signal.use_id,
            "scope": signal.scope.to_dict(),
            "category": str(category),
        }
        if note:
            payload["note"] = note
        return self._emit(ev.DRIFT_CLASSIFIED, payload, actor)

    def enforce_human_backup(
        self, *, release_id: str, use_id: str, reason: str, actor: str, signal_id: str | None = None
    ) -> dict:
        """升级为强制人工兜底。因漂移触发时，只接受“真实性能下降”分类。"""
        state = self._state()
        ru = self._require_live(state, release_id, use_id)
        self._require_real_degradation(state, signal_id)
        if ru.stage not in ONLINE_STAGES:
            raise GatekeeperError(f"用途 {use_id} 不在线，无需升级人工兜底")
        payload = {
            "release_id": release_id,
            "use_id": use_id,
            "scope": state.purposes[use_id]["scope"].to_dict(),
            "reason": reason,
        }
        if signal_id:
            payload["signal_id"] = signal_id
        return self._emit(ev.HUMAN_BACKUP_ENFORCED, payload, actor)

    def block_use(
        self,
        *,
        release_id: str,
        use_id: str,
        reasons: list[str],
        actor: str,
        signal_id: str | None = None,
    ) -> dict:
        """阻断单个用途。漂移触发时必须已有 REAL_DEGRADATION 分类结论。

        阻断半径严格限定在该 (release, use, scope)；同版本其他用途不动。
        """
        state = self._state()
        self._require_use(state, release_id, use_id)
        if not reasons:
            raise GatekeeperError("阻断必须给出原因")
        self._require_real_degradation(state, signal_id)
        payload = {
            "release_id": release_id,
            "use_id": use_id,
            "scope": state.purposes[use_id]["scope"].to_dict(),
            "reasons": list(reasons),
        }
        if signal_id:
            payload["signal_id"] = signal_id
        return self._emit(ev.USE_BLOCKED, payload, actor)

    @staticmethod
    def _require_real_degradation(state: GovernanceState, signal_id: str | None) -> None:
        if signal_id is None:
            return
        signal = state.signals.get(signal_id)
        if signal is None:
            raise GatekeeperError(f"漂移信号 {signal_id} 不存在")
        if signal.category is None:
            raise GatekeeperError("漂移信号尚未分类：先区分数据延迟/工作流变化/真实下降")
        if signal.category != DriftCategory.REAL_DEGRADATION:
            raise GatekeeperError(
                f"信号分类为 {signal.category}，不是真实性能下降，禁止阻断或回滚；"
                "数据延迟与工作流变化应走运维与观察流程"
            )

    # ── 8. 不良事件、回滚（病例留存）、召回 ────────────────────────────────

    def report_adverse_event(
        self, *, adverse_id: str, release_id: str, use_id: str, case_ref: str, severity: str, summary: str, actor: str
    ) -> dict:
        state = self._state()
        self._require_use(state, release_id, use_id)
        if any(a.adverse_id == adverse_id for a in state.adverse):
            raise GatekeeperError(f"不良事件 {adverse_id} 已登记")
        if not severity.strip() or not summary.strip():
            raise GatekeeperError("不良事件必须记录严重程度与摘要")
        payload = {
            "adverse_id": adverse_id,
            "release_id": release_id,
            "use_id": use_id,
            "case_ref": case_ref,
            "severity": severity,
            "summary": summary,
        }
        return self._emit(ev.ADVERSE_EVENT_REPORTED, payload, actor)

    def rollback(
        self,
        *,
        release_id: str,
        use_ids: list[str],
        reason: str,
        actor: str,
        affected_case_refs: list[str] | None = None,
        signal_id: str | None = None,
    ) -> dict:
        """回滚指定用途。系统自动核对受影响病例清单覆盖全部实际暴露病例——
        回滚可以撤回建议，但“曾影响哪些病例”必须永久保留。

        若凭漂移信号回滚，信号必须已被分类为 REAL_DEGRADATION；
        数据延迟/工作流变化不得作为回滚依据。委员会基于供应商通告等
        外部原因主动回滚时可不附信号。
        """
        state = self._state()
        if release_id not in state.releases:
            raise GatekeeperError(f"发布 {release_id} 不存在")
        if not use_ids:
            raise GatekeeperError("回滚必须指明用途，禁止无边界全停")
        if not reason.strip():
            raise GatekeeperError("回滚必须记录原因")
        self._require_real_degradation(state, signal_id)
        exposed = {
            e.case_ref
            for e in state.exposures_for(release_id, use_ids)
        }
        declared = set(affected_case_refs or exposed)
        missing = exposed - declared
        if missing:
            raise GatekeeperError(
                f"受影响病例清单不完整，缺少实际暴露病例：{sorted(missing)}"
            )
        # 只回滚处于在线/阻断/兜底状态的用途；已回滚的拒绝重复操作。
        for use_id in use_ids:
            ru = self._require_use(state, release_id, use_id)
            if ru.stage not in ONLINE_STAGES | {Stage.BLOCKED}:
                raise GatekeeperError(f"用途 {use_id} 阶段 {ru.stage}，不在可回滚状态")
        ordered_refs = sorted(declared)
        self._emit(
            ev.ROLLBACK_INITIATED,
            {
                "release_id": release_id,
                "use_ids": list(use_ids),
                "reason": reason,
                "affected_case_refs": ordered_refs,
            },
            actor,
        )
        return self._emit(
            ev.ROLLBACK_COMPLETED,
            {
                "release_id": release_id,
                "use_ids": list(use_ids),
                "affected_case_refs": ordered_refs,
            },
            actor,
        )

    def decide_recall(self, *, release_id: str, use_ids: list[str], reason: str, actor: str) -> dict:
        """委员会召回决定；召回不删除任何暴露与证据记录。"""
        state = self._state()
        if release_id not in state.releases:
            raise GatekeeperError(f"发布 {release_id} 不存在")
        if release_id in state.recalls:
            raise GatekeeperError("该发布已有召回决定；扩大召回范围须新立发布级决定流程")
        if not reason.strip():
            raise GatekeeperError("召回必须记录理由")
        for use_id in use_ids:
            self._require_use(state, release_id, use_id)
        return self._emit(
            ev.RECALL_DECIDED,
            {"release_id": release_id, "use_ids": list(use_ids), "reason": reason},
            actor,
        )

    # ── 9. 查询投影 ────────────────────────────────────────────────────────

    def state_snapshot(self) -> GovernanceState:
        return self._state()

    def release_dossier(self, release_id: str):
        from .projections import build_release_dossier

        return build_release_dossier(self._state(), release_id)

    def department_directory(self):
        from .projections import DepartmentDirectory

        return DepartmentDirectory(self._state())
