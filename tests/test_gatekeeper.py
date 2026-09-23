"""守门规则测试：以「儿科急诊胸片 + 基层全科分诊」为主要场景。"""
from __future__ import annotations

import datetime as dt
import unittest

from src.domain import (
    DriftCause,
    GateDecision,
    GateError,
    GoLiveWindow,
    CanaryPlan,
    MetricResult,
    ModelVersion,
    PlannedUse,
    ReviewMode,
    ReviewResponsibility,
    RiskTier,
    SubgroupSpec,
    Threshold,
    UseScope,
    Violation,
)
from src.events import EventStore, FixedClock
from src.gatekeeper import Gatekeeper

T0 = dt.datetime(2026, 9, 23, 9, 0, tzinfo=dt.timezone.utc)
WINDOW = GoLiveWindow(T0 - dt.timedelta(days=1), T0 + dt.timedelta(days=7))

MODEL_V1 = ModelVersion(
    provider="vision-vendor",
    model="cxr-assist",
    model_version="2026.08.1",
    prompt_version="p-41",
    knowledge_base_version="kb-2026-09-10",
)


def ped_use(**over) -> UseScope:
    base = dict(
        use_id="cxr-ped-ed",
        title="儿科急诊胸片初筛提示",
        task="胸片阳性征象提示，供医生复核",
        department="儿科急诊",
        population="0–14 岁急诊就诊患儿",
        device_protocols=("DR-A/v2", "DR-B/v2"),
        risk_tier=RiskTier.HIGH,
        review=ReviewResponsibility(
            role="儿科急诊主治",
            mode=ReviewMode.SIGN_OFF_REQUIRED,
            rationale="高风险人群，建议仅作提示，执行前医生签字",
        ),
        thresholds=(Threshold("sensitivity", 0.95),),
        subgroups=(
            SubgroupSpec(
                name="under_3",
                threshold=Threshold("sensitivity", 0.93),
                minimum_sample=100,
            ),
        ),
        minimum_sample=500,
    )
    base.update(over)
    return UseScope(**base)


def gp_use(**over) -> UseScope:
    base = dict(
        use_id="triage-gp",
        title="基层全科问诊分诊提示",
        task="问诊信息整理与就诊优先级提示",
        department="基层全科",
        population="成人普通门诊",
        risk_tier=RiskTier.STANDARD,
        review=ReviewResponsibility(
            role="全科值班医生", mode=ReviewMode.POST_HOC_REVIEW
        ),
        thresholds=(Threshold("appropriate_triage_rate", 0.90),),
        minimum_sample=300,
    )
    base.update(over)
    return UseScope(**base)


class World:
    """搭建一个已冻结、但尚未激活的发布。"""

    def __init__(self):
        self.clock = FixedClock(T0)
        self.store = EventStore(self.clock)
        self.gk = Gatekeeper(self.store)
        self.gk.register_use(ped_use())
        self.gk.register_use(gp_use())
        self.gk.plan_release(
            "R-2026-09",
            model=MODEL_V1,
            uses=[
                PlannedUse(
                    "cxr-ped-ed",
                    intended_fraction=0.10,
                    canary=CanaryPlan(max_fraction=0.10, case_cap=50),
                ),
                PlannedUse("triage-gp", intended_fraction=0.20),
            ],
            window=WINDOW,
            planned_by="发布经理 林",
        )
        self.gk.freeze_plan("R-2026-09", by="委员会秘书")

    def good_evidence(self, use_id: str, **over) -> None:
        if use_id == "cxr-ped-ed":
            payload = dict(
                cohort_id="CQ-ped-2026Q3",
                overall=[MetricResult("sensitivity", 0.962, 540)],
                sample_size=540,
                subgroups={"under_3": [MetricResult("sensitivity", 0.941, 132)]},
                device_protocols_verified=("DR-A/v2", "DR-B/v2"),
                evaluator="临床验证组",
            )
        else:
            payload = dict(
                cohort_id="CQ-gp-2026Q3",
                overall=[MetricResult("appropriate_triage_rate", 0.93, 410)],
                sample_size=410,
                evaluator="临床验证组",
            )
        payload.update(over)
        self.gk.record_validation("R-2026-09", use_id, **payload)

    def approve_ped(self, independent: bool = True) -> None:
        self.gk.grant_independent_approval(
            "R-2026-09",
            "cxr-ped-ed",
            approver_id="reviewer-zhao",
            approver_role="临床安全委员会独立审查人",
            independence_declared=independent,
            conditions=["仅 10% 灰度", "主治签字"],
        )


def violations(exc: GateError) -> list[Violation]:
    return exc.violations


class EnvelopeTest(unittest.TestCase):
    def test_events_carry_baseline_envelope(self):
        w = World()
        event = w.store.all()[0]
        d = event.as_dict()
        for field_ in ("event_id", "kind", "occurred_at", "subject_id", "version", "payload"):
            self.assertIn(field_, d)
        self.assertEqual(d["subject_id"], "clinical-agent-release-governance")
        self.assertEqual(d["version"], 1)
        self.assertEqual(d["kind"], "USE_REGISTERED")

    def test_plan_frozen_kind_matches_baseline_fixture(self):
        w = World()
        kinds = {e.kind.value for e in w.store.all()}
        self.assertIn("PLAN_FROZEN", kinds)
        self.assertEqual(w.store.all()[-1].kind.value, "PLAN_FROZEN")


class SignOffTest(unittest.TestCase):
    def test_automation_cannot_substitute_physician_signoff(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.good_evidence("triage-gp")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="发布经理 林")

        # 服务端没有任何「代签字」入口；未携带医生签字事实的暴露一律拒绝。
        with self.assertRaises(GateError) as cm:
            w.gk.record_exposure(
                "cxr-ped-ed",
                case_ref="CASE-1001",
                physician_id="dr-sun",
                purpose="clinical_decision_support",
                signed_off=False,
            )
        self.assertEqual(violations(cm.exception), [Violation.SIGN_OFF_RULE_MISSING])

        event = w.gk.record_exposure(
            "cxr-ped-ed",
            case_ref="CASE-1002",
            physician_id="dr-sun",
            purpose="clinical_decision_support",
            signed_off=True,
            fraction_bucket=0.10,
        )
        self.assertTrue(event.payload["signed_off"])
        self.assertEqual(event.payload["physician_id"], "dr-sun")


class HighRiskGateTest(unittest.TestCase):
    def test_high_risk_needs_independent_approval(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="发布经理 林")
        self.assertIn(Violation.INDEPENDENT_APPROVAL_MISSING, violations(cm.exception))

    def test_approver_must_be_independent(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped(independent=False)
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="发布经理 林")
        self.assertIn(Violation.APPROVER_NOT_INDEPENDENT, violations(cm.exception))

    def test_minimum_sample_enforced(self):
        w = World()
        w.good_evidence(
            "cxr-ped-ed",
            overall=[MetricResult("sensitivity", 0.97, 300)],
            sample_size=300,
            subgroups={"under_3": [MetricResult("sensitivity", 0.95, 105)]},
        )
        w.approve_ped()
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        self.assertIn(Violation.INSUFFICIENT_SAMPLE, violations(cm.exception))

    def test_standard_use_does_not_require_independent_approval(self):
        w = World()
        w.good_evidence("triage-gp")
        event = w.gk.activate("R-2026-09", "triage-gp", by="林")
        self.assertEqual(event.kind.value, "USE_ACTIVATED")


class EvidenceGateTest(unittest.TestCase):
    def test_overall_threshold_miss_blocks_use(self):
        w = World()
        w.good_evidence(
            "triage-gp",
            overall=[MetricResult("appropriate_triage_rate", 0.86, 400)],
            sample_size=400,
        )
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "triage-gp", by="林")
        self.assertIn(Violation.THRESHOLD_NOT_MET, violations(cm.exception))

    def test_subgroup_degradation_blocks_but_not_other_use(self):
        w = World()
        # 儿科总体达标，但 3 岁以下亚组退化。
        w.good_evidence(
            "cxr-ped-ed",
            subgroups={"under_3": [MetricResult("sensitivity", 0.88, 130)]},
        )
        w.approve_ped()
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        self.assertIn(Violation.SUBGROUP_BELOW_THRESHOLD, violations(cm.exception))

        # 同发布的基层全科用途证据独立、照常激活。
        w.good_evidence("triage-gp")
        w.gk.activate("R-2026-09", "triage-gp", by="林")
        self.assertEqual(w.gk.status("triage-gp"), GateDecision.ACTIVE)
        self.assertEqual(w.gk.status("cxr-ped-ed"), GateDecision.PENDING)

    def test_subgroup_sample_insufficient_blocks(self):
        w = World()
        w.good_evidence(
            "cxr-ped-ed",
            subgroups={"under_3": [MetricResult("sensitivity", 0.98, 40)]},
        )
        w.approve_ped()
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        self.assertIn(Violation.SUBGROUP_SAMPLE_INSUFFICIENT, violations(cm.exception))

    def test_data_leakage_blocks_only_related_use(self):
        w = World()
        w.good_evidence("cxr-ped-ed", data_leakage_found=True)
        w.approve_ped()
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        self.assertIn(Violation.DATA_LEAKAGE, violations(cm.exception))

        w.good_evidence("triage-gp")
        w.gk.activate("R-2026-09", "triage-gp", by="林")
        self.assertEqual(w.gk.status("triage-gp"), GateDecision.ACTIVE)

    def test_device_protocol_change_blocks_until_covered(self):
        w = World()
        w.good_evidence(
            "cxr-ped-ed", device_protocols_verified=("DR-A/v2",)
        )  # DR-B/v2 协议变了，未覆盖
        w.approve_ped()
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        self.assertIn(Violation.DEVICE_PROTOCOL_MISMATCH, violations(cm.exception))

    def test_cannot_validate_or_activate_before_freeze(self):
        store = EventStore(FixedClock(T0))
        gk = Gatekeeper(store)
        gk.register_use(gp_use())
        gk.plan_release(
            "R-x", model=MODEL_V1, uses=[PlannedUse("triage-gp", 0.2)],
            window=WINDOW, planned_by="林",
        )
        with self.assertRaises(GateError) as cm:
            gk.record_validation(
                "R-x", "triage-gp",
                cohort_id="c", overall=[MetricResult("appropriate_triage_rate", 0.95, 300)],
                sample_size=300, evaluator="e",
            )
        self.assertIn(Violation.PLAN_NOT_FROZEN, violations(cm.exception))


class DriftTriageTest(unittest.TestCase):
    def _active(self) -> World:
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.good_evidence("triage-gp")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        w.gk.activate("R-2026-09", "triage-gp", by="林")
        return w

    def test_data_delay_does_not_block(self):
        w = self._active()
        w.gk.raise_drift_signal(
            "SIG-1", "cxr-ped-ed", metric="sensitivity", observed=0.80,
            expected=0.96, observation_window="2026-09-20/22", raised_by="监控",
        )
        w.gk.triage_drift(
            "SIG-1", cause=DriftCause.DATA_DELAY, triaged_by="数据组",
            resolution="影像回传管道积压 6 小时，补齐后指标恢复",
        )
        self.assertEqual(w.gk.status("cxr-ped-ed"), GateDecision.ACTIVE)

    def test_workflow_change_does_not_block(self):
        w = self._active()
        w.gk.raise_drift_signal(
            "SIG-2", "triage-gp", metric="appropriate_triage_rate", observed=0.88,
            expected=0.93, observation_window="2026-09-18/22", raised_by="监控",
        )
        w.gk.triage_drift(
            "SIG-2", cause=DriftCause.WORKFLOW_CHANGE, triaged_by="流程组",
            resolution="新分诊表单改变了必填项口径，分母定义变化",
        )
        self.assertEqual(w.gk.status("triage-gp"), GateDecision.ACTIVE)

    def test_true_degradation_suspends_only_affected_use(self):
        w = self._active()
        w.gk.raise_drift_signal(
            "SIG-3", "cxr-ped-ed", metric="sensitivity", observed=0.82,
            expected=0.96, observation_window="2026-09-19/22", raised_by="监控",
        )
        w.gk.triage_drift(
            "SIG-3", cause=DriftCause.TRUE_DEGRADATION, triaged_by="安全组",
            affected_use_ids=("cxr-ped-ed",),
            resolution="复阅确认阳性漏诊增多",
        )
        self.assertEqual(w.gk.status("cxr-ped-ed"), GateDecision.SUSPENDED)
        # 同科室/同模型不是阻断半径；基层全科继续在线。
        self.assertEqual(w.gk.status("triage-gp"), GateDecision.ACTIVE)

        # 挂起期间不能再登记新暴露。
        with self.assertRaises(GateError) as cm:
            w.gk.record_exposure(
                "cxr-ped-ed", case_ref="CASE-9", physician_id="dr-sun",
                purpose="clinical_decision_support", signed_off=True,
            )
        self.assertIn(Violation.USE_NOT_ACTIVE, violations(cm.exception))


class RecallAndExposureTest(unittest.TestCase):
    def test_recall_blocks_future_but_keeps_affected_cases(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        for case in ("CASE-1", "CASE-2", "CASE-3"):
            w.gk.record_exposure(
                "cxr-ped-ed", case_ref=case, physician_id="dr-sun",
                purpose="clinical_decision_support", signed_off=True,
                fraction_bucket=0.10,
            )
        w.gk.recall_use(
            "cxr-ped-ed", reason=Violation.TRUE_DEGRADATION, by="安全委员会",
            committee_decision_ref="CSC-2026-042",
            evidence_ref="SIG-3", notes="3 岁以下漏诊率上升，委员会决定召回",
        )
        self.assertEqual(w.gk.status("cxr-ped-ed"), GateDecision.RECALLED)

        # 同版本不得重新激活，必须作为新用途重新登记验证。
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        self.assertIn(Violation.USE_RECALLED, violations(cm.exception))

        # 回滚后曾影响哪些病例仍然可查。
        cases = w.gk.affected_cases(use_id="cxr-ped-ed")
        self.assertEqual([c["case_ref"] for c in cases], ["CASE-1", "CASE-2", "CASE-3"])
        self.assertTrue(all(c["signed_off"] for c in cases))

    def test_recall_does_not_touch_other_use(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.good_evidence("triage-gp")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        w.gk.activate("R-2026-09", "triage-gp", by="林")
        w.gk.recall_use(
            "cxr-ped-ed", reason=Violation.TRUE_DEGRADATION, by="委员会",
            committee_decision_ref="CSC-2026-042",
        )
        self.assertEqual(w.gk.status("triage-gp"), GateDecision.ACTIVE)
        w.gk.record_exposure(
            "triage-gp", case_ref="GP-7", physician_id="dr-ma",
            purpose="clinical_decision_support", signed_off=False,
        )  # POST_HOC 不要求逐例签字

    def test_suspension_requires_revalidation_in_new_release(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        w.gk.suspend_use(
            "cxr-ped-ed", reason=Violation.DEVICE_PROTOCOL_MISMATCH, by="工程部",
            notes="DR-B 设备固件升级到 v3",
        )
        # 不能凭旧发布直接恢复。
        with self.assertRaises(GateError) as cm:
            w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        self.assertIn(Violation.REVALIDATION_REQUIRED, violations(cm.exception))

        # 新发布（新版本模型/提示词/知识库），并先按 v3 协议修订用途边界后重新验证。
        w.gk.amend_use_scope(
            ped_use(device_protocols=("DR-A/v2", "DR-B/v3")),
            by="工程部", reason="DR-B 设备固件升级到 v3，原 v2 协议验证不再覆盖",
        )
        w.gk.plan_release(
            "R-2026-10",
            model=ModelVersion(
                "vision-vendor", "cxr-assist", "2026.09.2", "p-44", "kb-2026-09-22"
            ),
            uses=[PlannedUse(
                "cxr-ped-ed", intended_fraction=0.05,
                canary=CanaryPlan(max_fraction=0.05, case_cap=20),
            )],
            window=GoLiveWindow(T0, T0 + dt.timedelta(days=14)),
            planned_by="林",
        )
        w.gk.freeze_plan("R-2026-10", by="秘书")
        w.gk.record_validation(
            "R-2026-10", "cxr-ped-ed",
            cohort_id="CQ-ped-2026Q3-recheck",
            overall=[MetricResult("sensitivity", 0.968, 520)],
            sample_size=520,
            subgroups={"under_3": [MetricResult("sensitivity", 0.95, 110)]},
            device_protocols_verified=("DR-A/v2", "DR-B/v3"),
            evaluator="临床验证组",
        )
        w.gk.grant_independent_approval(
            "R-2026-10", "cxr-ped-ed", approver_id="reviewer-zhao",
            approver_role="独立审查人", independence_declared=True,
        )
        w.gk.activate("R-2026-10", "cxr-ped-ed", by="林")
        self.assertEqual(w.gk.status("cxr-ped-ed"), GateDecision.ACTIVE)

    def test_sentinel_adverse_event_auto_suspends(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        w.gk.record_adverse_event(
            "AE-1", "cxr-ped-ed", case_ref="CASE-77", severity="SENTINEL",
            description="气胸漏诊并延误处置", reported_by="dr-sun",
        )
        self.assertEqual(w.gk.status("cxr-ped-ed"), GateDecision.SUSPENDED)


class PurposeBoundaryTest(unittest.TestCase):
    def test_patient_data_limited_to_authorized_purpose(self):
        w = World()
        w.good_evidence("triage-gp")
        w.gk.activate("R-2026-09", "triage-gp", by="林")

        with self.assertRaises(GateError) as cm:
            w.gk.record_exposure(
                "triage-gp", case_ref="CASE-50", physician_id="dr-ma",
                purpose="model_training", signed_off=False,
            )
        self.assertIn(Violation.PURPOSE_BOUNDARY_DENIED, violations(cm.exception))

        # 越权尝试本身留痕，可供审计。
        violations_logged = [
            e for e in w.store.all() if e.kind.value == "PURPOSE_BOUNDARY_VIOLATION"
        ]
        self.assertEqual(len(violations_logged), 1)
        self.assertEqual(violations_logged[0].payload["attempted_purpose"], "model_training")

        # 原授权目的内的使用不受影响。
        w.gk.record_exposure(
            "triage-gp", case_ref="CASE-51", physician_id="dr-ma",
            purpose="clinical_decision_support", signed_off=False,
        )


class CanaryWindowTest(unittest.TestCase):
    def _ready(self) -> World:
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        return w

    def test_activate_outside_window_denied(self):
        w = self._ready()
        with self.assertRaises(GateError) as cm:
            w.gk.activate(
                "R-2026-09", "cxr-ped-ed", by="林",
                at=T0 + dt.timedelta(days=30),
            )
        self.assertIn(Violation.OUTSIDE_GO_LIVE_WINDOW, violations(cm.exception))

    def test_exposure_beyond_canary_fraction_denied(self):
        w = self._ready()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        with self.assertRaises(GateError) as cm:
            w.gk.record_exposure(
                "cxr-ped-ed", case_ref="CASE-1", physician_id="dr-sun",
                purpose="clinical_decision_support", signed_off=True,
                fraction_bucket=0.50,
            )
        self.assertIn(Violation.CANARY_LIMIT_EXCEEDED, violations(cm.exception))

    def test_canary_case_cap_enforced(self):
        w = self._ready()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        for i in range(50):
            w.gk.record_exposure(
                "cxr-ped-ed", case_ref=f"CASE-{i}", physician_id="dr-sun",
                purpose="clinical_decision_support", signed_off=True,
                fraction_bucket=0.10,
            )
        with self.assertRaises(GateError) as cm:
            w.gk.record_exposure(
                "cxr-ped-ed", case_ref="CASE-50", physician_id="dr-sun",
                purpose="clinical_decision_support", signed_off=True,
                fraction_bucket=0.10,
            )
        self.assertIn(Violation.CANARY_LIMIT_EXCEEDED, violations(cm.exception))

    def test_intended_fraction_above_canary_blocks_activation(self):
        store = EventStore(FixedClock(T0))
        gk = Gatekeeper(store)
        gk.register_use(ped_use())
        gk.plan_release(
            "R-wide", model=MODEL_V1,
            uses=[PlannedUse("cxr-ped-ed", intended_fraction=0.80,
                             canary=CanaryPlan(max_fraction=0.10))],
            window=WINDOW, planned_by="林",
        )
        gk.freeze_plan("R-wide", by="秘书")
        gk.record_validation(
            "R-wide", "cxr-ped-ed", cohort_id="c",
            overall=[MetricResult("sensitivity", 0.97, 600)], sample_size=600,
            subgroups={"under_3": [MetricResult("sensitivity", 0.95, 120)]},
            device_protocols_verified=("DR-A/v2", "DR-B/v2"), evaluator="e",
        )
        gk.grant_independent_approval(
            "R-wide", "cxr-ped-ed", approver_id="r", approver_role="x",
            independence_declared=True,
        )
        with self.assertRaises(GateError) as cm:
            gk.activate("R-wide", "cxr-ped-ed", by="林")
        self.assertIn(Violation.CANARY_LIMIT_EXCEEDED, violations(cm.exception))


class ScopeAmendmentTest(unittest.TestCase):
    def test_active_scope_cannot_be_amended(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        with self.assertRaises(GateError) as cm:
            w.gk.amend_use_scope(ped_use(), by="工程部", reason="在线期间改边界")
        self.assertIn(Violation.USE_ALREADY_ACTIVE, violations(cm.exception))

    def test_recalled_scope_cannot_be_reused_via_amendment(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        w.gk.recall_use(
            "cxr-ped-ed", reason=Violation.TRUE_DEGRADATION, by="委员会",
            committee_decision_ref="CSC-9",
        )
        with self.assertRaises(GateError) as cm:
            w.gk.amend_use_scope(ped_use(), by="工程部", reason="想换个阈值复活")
        self.assertIn(Violation.USE_RECALLED, violations(cm.exception))

    def test_suspended_scope_amendment_is_recorded(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        w.gk.suspend_use(
            "cxr-ped-ed", reason=Violation.DEVICE_PROTOCOL_MISMATCH, by="工程部",
        )
        w.gk.amend_use_scope(
            ped_use(device_protocols=("DR-A/v2", "DR-B/v3")),
            by="工程部", reason="DR-B 固件升级 v3",
        )
        use = w.gk.state["uses"]["cxr-ped-ed"]
        self.assertEqual(use["device_protocols"], ["DR-A/v2", "DR-B/v3"])
        self.assertEqual(len(use["amendments"]), 1)
        self.assertEqual(use["status"], "SUSPENDED")


class QueryViewTest(unittest.TestCase):
    def test_release_dossier_reconstructs_evidence_approval_exposure(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.good_evidence("triage-gp")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        w.gk.activate("R-2026-09", "triage-gp", by="林")
        w.gk.record_exposure(
            "cxr-ped-ed", case_ref="CASE-1", physician_id="dr-sun",
            purpose="clinical_decision_support", signed_off=True,
            fraction_bucket=0.10,
        )

        dossier = w.gk.release_dossier("R-2026-09")
        self.assertEqual(dossier["model"], MODEL_V1.as_dict())
        ped = dossier["uses"]["cxr-ped-ed"]
        self.assertEqual(ped["scope"]["department"], "儿科急诊")
        self.assertEqual(ped["evidence"]["sample_size"], 540)
        self.assertEqual(ped["evidence"]["cohort_id"], "CQ-ped-2026Q3")
        self.assertEqual(ped["independent_approval"]["approver_id"], "reviewer-zhao")
        self.assertIsNotNone(ped["activation"])
        self.assertEqual(ped["exposed_cases"], ["CASE-1"])
        self.assertEqual(ped["exposure_count"], 1)
        # 暴露记录携带实际使用的模型/提示词/知识库版本。
        self.assertEqual(ped["exposures"][0]["model"]["prompt_version"], "p-41")

    def test_department_guide_shows_allowed_and_fallback(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        guide = w.gk.department_guide("儿科急诊")
        self.assertEqual(len(guide["allowed_now"]), 1)
        entry = guide["allowed_now"][0]
        self.assertEqual(entry["use_id"], "cxr-ped-ed")
        self.assertTrue(entry["human_fallback_required"])
        self.assertEqual(entry["review"]["mode"], "SIGN_OFF_REQUIRED")
        self.assertEqual(entry["model"]["model_version"], "2026.08.1")

        w.gk.recall_use(
            "cxr-ped-ed", reason=Violation.TRUE_DEGRADATION, by="委员会",
            committee_decision_ref="CSC-1",
        )
        guide = w.gk.department_guide("儿科急诊")
        self.assertEqual(guide["allowed_now"], [])
        self.assertEqual(guide["suspended_or_recalled"][0]["reason"],
                         Violation.TRUE_DEGRADATION.value)

    def test_event_stream_replays_to_same_state(self):
        w = World()
        w.good_evidence("cxr-ped-ed")
        w.approve_ped()
        w.gk.activate("R-2026-09", "cxr-ped-ed", by="林")
        w.gk.record_exposure(
            "cxr-ped-ed", case_ref="CASE-1", physician_id="dr-sun",
            purpose="clinical_decision_support", signed_off=True,
            fraction_bucket=0.10,
        )
        w.gk.suspend_use(
            "cxr-ped-ed", reason=Violation.TRUE_DEGRADATION, by="安全组",
            evidence_ref="SIG-3",
        )

        # 用空守门人重放完整事件流，状态与卷宗应完全一致。
        replay_store = EventStore(FixedClock(T0))
        replay_store.replay(w.store.all())
        replayed = Gatekeeper(replay_store)
        self.assertEqual(replayed.status("cxr-ped-ed"), GateDecision.SUSPENDED)
        dossier = replayed.release_dossier("R-2026-09")
        self.assertEqual(dossier["uses"]["cxr-ped-ed"]["exposed_cases"], ["CASE-1"])
        self.assertEqual(
            dossier["uses"]["cxr-ped-ed"]["block_history"][0]["reason"],
            Violation.TRUE_DEGRADATION.value,
        )


if __name__ == "__main__":
    unittest.main()
