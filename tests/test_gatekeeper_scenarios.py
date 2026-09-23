"""端到端守门场景测试。

覆盖需求中的每条硬性规则：
1. 同一模型跨科室/人群独立验证（基层全科 ≠ 儿科急诊）；
2. 高风险用途独立批准 + 最小样本；
3. 泄漏 / 设备协议变化 / 亚组退化只阻断相关用途；
4. 漂移三分类：数据延迟与工作流变化不得触发阻断；
5. 回滚后“曾影响哪些病例”必须保留且清单完整；
6. 一次发布可还原证据、审批与实际暴露；
7. 科室目录回答“能用在哪、何时必须人工兜底”；
8. 患者数据限定原授权目的；
9. 医生签字不可替代，覆盖必须给理由；
10. 召回不删除记录。
"""

import unittest

from src.domain import Decision, DriftCategory, GateFailure, ReviewMode, RiskClass, Scope, Stage
from src.gatekeeper import GatekeeperError
from src.projections import DepartmentDirectory, build_release_dossier

from tests._fixtures import (
    approve,
    build_gatekeeper,
    canary_use,
    passing_subgroups,
    register_gp_purpose,
    register_ped_purpose,
    register_two_use_candidate,
    schedule_and_canary,
    schedule_use,
    submit_passing_validation,
)


def prepare_approved_release(gk, release_id="R1"):
    """登记两用途 → 候选 → 双用途验证通过 → 双用途批准。"""
    register_gp_purpose(gk)
    register_ped_purpose(gk)
    register_two_use_candidate(gk, release_id=release_id)
    self_failed = []
    assert submit_passing_validation(gk, "U-GP", release_id=release_id) == []
    assert submit_passing_validation(gk, "U-PED", release_id=release_id) == []
    approve(gk, "U-GP", release_id=release_id, approver="dr-chair")
    approve(gk, "U-PED", release_id=release_id, approver="dr-vice")
    return release_id


class IndependentValidationTest(unittest.TestCase):
    def setUp(self):
        self.gk = build_gatekeeper()

    def test_gp_stability_does_not_clear_pediatric_ed(self):
        # 基层全科验证通过，不等于儿科急诊可以上线。
        prepare_approved_release(self.gk)
        state = self.gk.state_snapshot()
        self.assertEqual(state.use("R1", "U-GP").stage, Stage.APPROVED)
        self.assertEqual(state.use("R1", "U-PED").stage, Stage.APPROVED)

        # 新建一个只在基层全科有证据、却试图带上儿科急诊的发布——
        # 儿科证据缺失亚组 → 闸门失败，全科不受影响。
        register_two_use_candidate(self.gk, release_id="R2", prompt_version="p-42")
        assert submit_passing_validation(self.gk, "U-GP", release_id="R2") == []
        purpose_ped = state.purposes["U-PED"]
        reasons = self.gk.submit_validation(
            release_id="R2",
            use_id="U-PED",
            cohort_id="cohort-ped-weak",
            dataset_version="ds-2026q3",
            dataset_purpose=purpose_ped["data_purpose"],
            sample_size=600,
            overall_metrics={"sensitivity": 0.98},
            subgroup_results={},  # 关键亚组无结果
            leakage_screen_clear=True,
            device_protocol="DEFAULT",
            submitted_by="analyst-1",
            actor="analyst-1",
        )
        self.assertIn(GateFailure.MISSING_SUBGROUP, reasons)
        self.assertEqual(self.gk.state_snapshot().use("R2", "U-GP").stage, Stage.VALIDATED)
        self.assertEqual(self.gk.state_snapshot().use("R2", "U-PED").stage, Stage.CANDIDATE)
        # 未过闸门的儿科用途不能被批准。
        with self.assertRaises(GatekeeperError):
            approve(self.gk, "U-PED", release_id="R2", approver="dr-vice")

    def test_prompt_change_requires_revalidation(self):
        # 提示词从 p-41 调整到 p-42：旧版本证据不能沿用到新候选。
        prepare_approved_release(self.gk)
        register_two_use_candidate(self.gk, release_id="R2", prompt_version="p-42")
        # R2 上任何用途在新证据提交前都是候选态。
        self.assertEqual(self.gk.state_snapshot().use("R2", "U-GP").stage, Stage.CANDIDATE)


class HighRiskGuardrailsTest(unittest.TestCase):
    def setUp(self):
        self.gk = build_gatekeeper()

    def test_high_risk_min_sample_enforced_at_registration(self):
        with self.assertRaises(GatekeeperError):
            register_ped_purpose(self.gk, use_id="U-PED-LOW", min_sample=100, min_subgroup_sample=20)

    def test_high_risk_requires_independent_approval(self):
        register_gp_purpose(self.gk)
        register_ped_purpose(self.gk)
        register_two_use_candidate(self.gk)
        submit_passing_validation(self.gk, "U-PED")
        with self.assertRaises(GatekeeperError):
            self.gk.committee_decide(
                release_id="R1",
                use_id="U-PED",
                decision=Decision.APPROVE,
                approver_id="someone",
                independent=False,  # 非独立
                rationale="同意",
                actor="someone",
            )

    def test_approver_must_not_be_validation_submitter(self):
        register_gp_purpose(self.gk)
        register_ped_purpose(self.gk)
        register_two_use_candidate(self.gk)
        submit_passing_validation(self.gk, "U-PED")
        with self.assertRaises(GatekeeperError):
            self.gk.committee_decide(
                release_id="R1",
                use_id="U-PED",
                decision=Decision.APPROVE,
                approver_id="analyst-1",  # 即验证提交人
                independent=True,
                rationale="自我批准",
                actor="analyst-1",
            )

    def test_validation_sample_below_floor_blocks_gate(self):
        register_gp_purpose(self.gk)
        register_ped_purpose(self.gk)
        register_two_use_candidate(self.gk)
        purpose = self.gk.state_snapshot().purposes["U-PED"]
        reasons = self.gk.submit_validation(
            release_id="R1",
            use_id="U-PED",
            cohort_id="c",
            dataset_version="ds-2026q3",
            dataset_purpose=purpose["data_purpose"],
            sample_size=450,  # < 500
            overall_metrics={"sensitivity": 0.99},
            subgroup_results=passing_subgroups("U-PED"),
            leakage_screen_clear=True,
            device_protocol="DEFAULT",
            submitted_by="analyst-1",
            actor="analyst-1",
        )
        self.assertIn(GateFailure.SAMPLE_TOO_SMALL, reasons)


class GateFailuresScopeIsolationTest(unittest.TestCase):
    """泄漏 / 设备协议 / 亚组退化只阻断相关用途，其他已证明安全的场景继续运行。"""

    def setUp(self):
        self.gk = build_gatekeeper()
        prepare_approved_release(self.gk)
        schedule_and_canary(self.gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        self.gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        ped_scope = Scope("儿科急诊", "0-14岁")
        schedule_use(self.gk, "U-PED", ped_scope)
        canary_use(self.gk, "U-PED", ped_scope)

    def test_leakage_blocks_only_related_use(self):
        # R2 中儿科队列发现数据泄漏；全科用途仍可独立验证通过并继续。
        register_two_use_candidate(self.gk, release_id="R2", model_version="m-2.4.0")
        gp_reasons = submit_passing_validation(self.gk, "U-GP", release_id="R2")
        self.assertEqual(gp_reasons, [])
        purpose = self.gk.state_snapshot().purposes["U-PED"]
        ped_reasons = self.gk.submit_validation(
            release_id="R2", use_id="U-PED", cohort_id="c-ped",
            dataset_version="ds-2026q3", dataset_purpose=purpose["data_purpose"],
            sample_size=600, overall_metrics={"sensitivity": 0.99},
            subgroup_results=passing_subgroups("U-PED"),
            leakage_screen_clear=False,  # 泄漏
            device_protocol="DEFAULT", submitted_by="analyst-2", actor="analyst-2",
        )
        self.assertEqual(ped_reasons, [GateFailure.LEAKAGE])
        # R1 的全科在线暴露不受影响。
        self.gk.record_exposure(
            release_id="R1", use_id="U-GP", case_ref="CASE-100", recommendation_ref="R-100",
            signoff_physician_id="dr-a", review_confirmed=True, actor="ward",
        )

    def test_device_protocol_change_blocks_only_matching_scope(self):
        register_two_use_candidate(self.gk, release_id="R2", knowledge_base_version="kb-10")
        purpose = self.gk.state_snapshot().purposes["U-PED"]
        reasons = self.gk.submit_validation(
            release_id="R2", use_id="U-PED", cohort_id="c-ped",
            dataset_version="ds-2026q3", dataset_purpose=purpose["data_purpose"],
            sample_size=600, overall_metrics={"sensitivity": 0.99},
            subgroup_results=passing_subgroups("U-PED"),
            leakage_screen_clear=True,
            device_protocol="DR-PORTRAIT-V2",  # 设备协议变了
            submitted_by="analyst-2", actor="analyst-2",
        )
        self.assertEqual(reasons, [GateFailure.DEVICE_PROTOCOL])
        # 全科 R1 依旧 ACTIVE。
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-GP").stage, Stage.ACTIVE)

    def test_subgroup_degradation_blocks_ped_only(self):
        register_two_use_candidate(self.gk, release_id="R2", model_version="m-2.4.0")
        purpose = self.gk.state_snapshot().purposes["U-PED"]
        degraded = {
            "0-3岁": {"sample_size": 200, "metrics": {"sensitivity": 0.90}},  # < 0.97
            "4-14岁": {"sample_size": 400, "metrics": {"sensitivity": 0.99}},
        }
        reasons = self.gk.submit_validation(
            release_id="R2", use_id="U-PED", cohort_id="c-ped",
            dataset_version="ds-2026q3", dataset_purpose=purpose["data_purpose"],
            sample_size=600, overall_metrics={"sensitivity": 0.98},
            subgroup_results=degraded,
            leakage_screen_clear=True, device_protocol="DEFAULT",
            submitted_by="analyst-2", actor="analyst-2",
        )
        self.assertIn(GateFailure.SUBGROUP_DEGRADATION, reasons)
        self.assertNotIn(GateFailure.THRESHOLD_MISS, reasons)  # 总体达标但亚组退化
        self.assertEqual(submit_passing_validation(self.gk, "U-GP", release_id="R2"), [])


class DriftClassificationTest(unittest.TestCase):
    def setUp(self):
        self.gk = build_gatekeeper()
        prepare_approved_release(self.gk)
        schedule_and_canary(self.gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        self.gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        for i in range(3):
            self.gk.record_exposure(
                release_id="R1", use_id="U-GP", case_ref=f"CASE-{i}", recommendation_ref=f"R-{i}",
                signoff_physician_id="dr-a", review_confirmed=True, actor="ward",
            )

    def _raise(self, signal_id):
        self.gk.raise_drift_signal(
            signal_id=signal_id, release_id="R1", use_id="U-GP",
            scope=Scope("全科门诊", "成人"), metric="sensitivity",
            observed=0.82, expected=0.95, actor="monitor",
        )

    def test_data_delay_must_not_trigger_block(self):
        self._raise("S-DELAY")
        self.gk.classify_drift(signal_id="S-DELAY", category=DriftCategory.DATA_DELAY, actor="sre")
        with self.assertRaises(GatekeeperError):
            self.gk.block_use(release_id="R1", use_id="U-GP", reasons=["指标下滑"],
                              signal_id="S-DELAY", actor="committee")
        # 用途仍在线。
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-GP").stage, Stage.ACTIVE)

    def test_workflow_change_must_not_trigger_rollback(self):
        self._raise("S-WF")
        self.gk.classify_drift(signal_id="S-WF", category=DriftCategory.WORKFLOW_CHANGE, actor="sre")
        with self.assertRaises(GatekeeperError):
            self.gk.rollback(release_id="R1", use_ids=["U-GP"], reason="口径变化",
                             affected_case_refs=["CASE-0", "CASE-1", "CASE-2"],
                             signal_id="S-WF", actor="committee")

    def test_committee_may_rollback_without_signal_for_external_notice(self):
        # 无漂移信号时，委员会可基于供应商缺陷通告主动回滚，但病例清单仍须完整。
        cases = ["CASE-0", "CASE-1", "CASE-2"]
        self.gk.rollback(release_id="R1", use_ids=["U-GP"], reason="供应商通告模型缺陷",
                         affected_case_refs=cases, actor="committee")
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-GP").stage, Stage.ROLLED_BACK)

    def test_unclassified_signal_cannot_block(self):
        self._raise("S-RAW")
        with self.assertRaises(GatekeeperError):
            self.gk.block_use(release_id="R1", use_id="U-GP", reasons=["未分类"],
                              signal_id="S-RAW", actor="committee")

    def test_real_degradation_allows_block_and_other_use_kept(self):
        # 同时在线的儿科灰度不受全科阻断影响。
        ped_scope = Scope("儿科急诊", "0-14岁")
        schedule_use(self.gk, "U-PED", ped_scope)
        canary_use(self.gk, "U-PED", ped_scope)
        self._raise("S-REAL")
        self.gk.classify_drift(signal_id="S-REAL", category=DriftCategory.REAL_DEGRADATION, actor="sre")
        self.gk.block_use(release_id="R1", use_id="U-GP", reasons=["SUBGROUP_DEGRADATION"],
                          signal_id="S-REAL", actor="committee")
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-GP").stage, Stage.BLOCKED)
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-PED").stage, Stage.CANARY)
        # 被阻断用途禁止继续暴露。
        with self.assertRaises(GatekeeperError):
            self.gk.record_exposure(
                release_id="R1", use_id="U-GP", case_ref="CASE-X", recommendation_ref="R-X",
                signoff_physician_id="dr-a", review_confirmed=True, actor="ward",
            )

    def test_real_degradation_can_enforce_human_backup(self):
        self._raise("S-BACKUP")
        self.gk.classify_drift(signal_id="S-BACKUP", category=DriftCategory.REAL_DEGRADATION, actor="sre")
        self.gk.enforce_human_backup(release_id="R1", use_id="U-GP", reason="指标逼近阈值",
                                     signal_id="S-BACKUP", actor="committee")
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-GP").stage, Stage.HUMAN_BACKUP)
        # 即使该用途原本只要求抽查，兜底阶段也必须逐例确认复核。
        with self.assertRaises(GatekeeperError):
            self.gk.record_exposure(
                release_id="R1", use_id="U-GP", case_ref="CASE-B1", recommendation_ref="R-B1",
                signoff_physician_id="dr-a", review_confirmed=False, actor="ward",
            )
        self.gk.record_exposure(
            release_id="R1", use_id="U-GP", case_ref="CASE-B1", recommendation_ref="R-B1",
            signoff_physician_id="dr-a", review_confirmed=True, actor="ward",
        )

    def test_human_backup_cannot_silently_return_to_full_rollout(self):
        self._raise("S-B2")
        self.gk.classify_drift(signal_id="S-B2", category=DriftCategory.REAL_DEGRADATION, actor="sre")
        self.gk.enforce_human_backup(release_id="R1", use_id="U-GP", reason="退化",
                                     signal_id="S-B2", actor="committee")
        # 不能靠激活命令直接拿掉人工防线。
        with self.assertRaises(GatekeeperError):
            self.gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        # 恢复路径是新版本重新验证、重新批准。
        register_two_use_candidate(self.gk, release_id="R2", model_version="m-2.4.1")
        assert submit_passing_validation(self.gk, "U-GP", release_id="R2") == []
        approve(self.gk, "U-GP", release_id="R2", approver="dr-chair")
        r2_scope = Scope("全科门诊", "成人")
        schedule_use(self.gk, "U-GP", r2_scope, release_id="R2")
        canary_use(self.gk, "U-GP", r2_scope, release_id="R2")
        self.gk.activate_use(release_id="R2", use_id="U-GP", actor="ops")
        self.assertEqual(self.gk.state_snapshot().use("R2", "U-GP").stage, Stage.ACTIVE)
        # 旧版本被标记取代，目录中同一用途只剩 R2；但 R1 档案仍完整可查。
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-GP").stage, Stage.SUPERSEDED)
        gp = self.gk.department_directory().for_department("全科门诊")
        self.assertEqual([e["release_id"] for e in gp], ["R2"])
        self.assertEqual(self.gk.release_dossier("R1").superseded["U-GP"], "R2")


class SignoffAndOverrideTest(unittest.TestCase):
    def setUp(self):
        self.gk = build_gatekeeper()
        prepare_approved_release(self.gk)

    def test_exposure_requires_physician_signoff(self):
        schedule_and_canary(self.gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        self.gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        with self.assertRaises(GatekeeperError):
            self.gk.record_exposure(
                release_id="R1", use_id="U-GP", case_ref="CASE-1", recommendation_ref="R-1",
                signoff_physician_id="", review_confirmed=True, actor="ward",
            )

    def test_required_review_mode_blocks_unconfirmed_exposure(self):
        # 儿科：逐例复核，未确认复核不能记账。
        schedule_and_canary(self.gk, use_ids=["U-PED"], canary_scope=Scope("儿科急诊", "0-14岁"))
        with self.assertRaises(GatekeeperError):
            self.gk.record_exposure(
                release_id="R1", use_id="U-PED", case_ref="P-CASE-1", recommendation_ref="P-R-1",
                signoff_physician_id="dr-ped", review_confirmed=False, actor="ward",
            )
        self.gk.record_exposure(
            release_id="R1", use_id="U-PED", case_ref="P-CASE-1", recommendation_ref="P-R-1",
            signoff_physician_id="dr-ped", review_confirmed=True, actor="ward",
        )

    def test_override_requires_reason(self):
        schedule_and_canary(self.gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        self.gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        with self.assertRaises(GatekeeperError):
            self.gk.record_exposure(
                release_id="R1", use_id="U-GP", case_ref="CASE-1", recommendation_ref="R-1",
                signoff_physician_id="dr-a", review_confirmed=True,
                override_physician_id="dr-b", actor="ward",
            )
        self.gk.record_exposure(
            release_id="R1", use_id="U-GP", case_ref="CASE-1", recommendation_ref="R-1",
            signoff_physician_id="dr-a", review_confirmed=True,
            override_physician_id="dr-b", override_reason="影像复核为阴性，模型误报", actor="ward",
        )

    def test_canary_exposure_confined_to_canary_scope(self):
        schedule_and_canary(self.gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        with self.assertRaises(GatekeeperError):
            self.gk.record_exposure(
                release_id="R1", use_id="U-GP", case_ref="CASE-1", recommendation_ref="R-1",
                signoff_physician_id="dr-a", review_confirmed=True, actor="ward",
                scope=Scope("儿科急诊", "0-14岁"),
            )


class RollbackMemoryTest(unittest.TestCase):
    def setUp(self):
        self.gk = build_gatekeeper()
        prepare_approved_release(self.gk)
        schedule_and_canary(self.gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        self.gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        for i in range(5):
            self.gk.record_exposure(
                release_id="R1", use_id="U-GP", case_ref=f"CASE-{i}", recommendation_ref=f"R-{i}",
                signoff_physician_id="dr-a", review_confirmed=True, actor="ward",
            )

    def test_rollback_requires_complete_case_list(self):
        with self.assertRaises(GatekeeperError):
            self.gk.rollback(
                release_id="R1", use_ids=["U-GP"], reason="真实退化",
                affected_case_refs=["CASE-0", "CASE-1"],  # 漏报 3 例
                actor="committee",
            )

    def test_rollback_preserves_affected_cases(self):
        cases = [f"CASE-{i}" for i in range(5)]
        self.gk.rollback(release_id="R1", use_ids=["U-GP"], reason="真实性能下降",
                         affected_case_refs=cases, actor="committee")
        dossier = self.gk.release_dossier("R1")
        self.assertTrue(dossier.rollback["completed"])
        self.assertEqual(dossier.affected_cases_after_rollback, cases)
        # 暴露明细仍可在档案中查到。
        exposed = [e.case_ref for e in dossier.exposures["U-GP"]]
        self.assertEqual(sorted(exposed), cases)
        # 回滚后禁止继续暴露。
        with self.assertRaises(GatekeeperError):
            self.gk.record_exposure(
                release_id="R1", use_id="U-GP", case_ref="CASE-NEW", recommendation_ref="R-N",
                signoff_physician_id="dr-a", review_confirmed=True, actor="ward",
            )

    def test_rollback_scoped_to_use_leaves_other_use_running(self):
        # 儿科同时灰度中；回滚全科不得连带儿科。
        ped_scope = Scope("儿科急诊", "0-14岁")
        schedule_use(self.gk, "U-PED", ped_scope)
        canary_use(self.gk, "U-PED", ped_scope)
        cases = [f"CASE-{i}" for i in range(5)]
        self.gk.rollback(release_id="R1", use_ids=["U-GP"], reason="真实退化",
                         affected_case_refs=cases, actor="committee")
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-GP").stage, Stage.ROLLED_BACK)
        self.assertEqual(self.gk.state_snapshot().use("R1", "U-PED").stage, Stage.CANARY)

    def test_two_rollback_batches_keep_both_case_lists(self):
        # 先回滚全科，随后儿科也回滚：两批病例清单都必须在档案中保留。
        ped_scope = Scope("儿科急诊", "0-14岁")
        schedule_use(self.gk, "U-PED", ped_scope)
        canary_use(self.gk, "U-PED", ped_scope)
        self.gk.record_exposure(
            release_id="R1", use_id="U-PED", case_ref="P-CASE-7", recommendation_ref="P-R-7",
            signoff_physician_id="dr-ped", review_confirmed=True, actor="ward",
        )
        gp_cases = [f"CASE-{i}" for i in range(5)]
        self.gk.rollback(release_id="R1", use_ids=["U-GP"], reason="全科真实退化",
                         affected_case_refs=gp_cases, actor="committee")
        self.gk.rollback(release_id="R1", use_ids=["U-PED"], reason="儿科独立问题",
                         affected_case_refs=["P-CASE-7"], actor="committee")

        dossier = self.gk.release_dossier("R1")
        merged = dossier.affected_cases_after_rollback
        self.assertIn("CASE-0", merged)
        self.assertIn("P-CASE-7", merged)
        self.assertEqual(len(dossier.rollback["batches"]), 2)
        self.assertEqual(sorted(dossier.rollback["use_ids"]), ["U-GP", "U-PED"])


class DataPurposeTest(unittest.TestCase):
    def test_dataset_used_outside_authorized_purpose_fails_gate(self):
        gk = build_gatekeeper()
        prepare_approved_release(gk)
        register_two_use_candidate(gk, release_id="R2", knowledge_base_version="kb-10")
        purpose = gk.state_snapshot().purposes["U-PED"]
        reasons = gk.submit_validation(
            release_id="R2", use_id="U-PED", cohort_id="c",
            dataset_version="ds-2026q3",
            dataset_purpose="商业模型训练",  # 超出原授权目的
            sample_size=600, overall_metrics={"sensitivity": 0.99},
            subgroup_results=passing_subgroups("U-PED"),
            leakage_screen_clear=True, device_protocol="DEFAULT",
            submitted_by="analyst-1", actor="analyst-1",
        )
        self.assertIn(GateFailure.PURPOSE_MISMATCH, reasons)
        self.assertEqual(purpose["data_purpose"], "儿科急诊影像辅助验证")

    def test_dataset_version_mismatch_fails_gate(self):
        gk = build_gatekeeper()
        prepare_approved_release(gk)
        register_two_use_candidate(gk, release_id="R2", dataset_version="ds-2026q4")
        purpose = gk.state_snapshot().purposes["U-GP"]
        reasons = gk.submit_validation(
            release_id="R2", use_id="U-GP", cohort_id="c",
            dataset_version="ds-2026q3",  # 旧数据版本
            dataset_purpose=purpose["data_purpose"],
            sample_size=300, overall_metrics={"sensitivity": 0.96},
            subgroup_results=passing_subgroups("U-GP"),
            leakage_screen_clear=True, device_protocol="DEFAULT",
            submitted_by="an", actor="an",
        )
        self.assertIn(GateFailure.DATASET_VERSION_MISMATCH, reasons)


class DossierAndDirectoryTest(unittest.TestCase):
    def test_dossier_reconstructs_evidence_approval_exposure(self):
        gk = build_gatekeeper()
        prepare_approved_release(gk)
        schedule_and_canary(gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        gk.record_exposure(release_id="R1", use_id="U-GP", case_ref="CASE-1",
                           recommendation_ref="R-1", signoff_physician_id="dr-a",
                           review_confirmed=True, actor="ward")
        gk.report_adverse_event(release_id="R1", use_id="U-GP", adverse_id="AE-1",
                                case_ref="CASE-1", severity="中度", summary="提示延迟", actor="risk")

        dossier = build_release_dossier(gk.state_snapshot(), "R1")
        kinds = [s.kind for s in dossier.timeline]
        # 关键证据环节都能从事件流还原。
        self.assertIn("VERSION_CANDIDATE_REGISTERED", kinds)
        self.assertIn("VALIDATION_PASSED", kinds)
        self.assertIn("COMMITTEE_DECISION", kinds)
        self.assertIn("EXPOSURE_RECORDED", kinds)
        self.assertIn("ADVERSE_EVENT_REPORTED", kinds)
        summary = dossier.summary()
        self.assertEqual(summary["version_fingerprint"]["prompt_version"], "p-41")
        self.assertEqual(summary["exposure_counts"]["U-GP"], 1)
        self.assertEqual(dossier.approvals["U-PED"]["approver_id"], "dr-vice")
        self.assertEqual(dossier.adverse["U-GP"][0]["adverse_id"], "AE-1")

    def test_directory_reports_allowed_use_and_backup_rules(self):
        gk = build_gatekeeper()
        prepare_approved_release(gk)
        # 全科全量、儿科灰度。
        schedule_and_canary(gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        ped_scope = Scope("儿科急诊", "0-14岁")
        schedule_use(gk, "U-PED", ped_scope)
        canary_use(gk, "U-PED", ped_scope)

        directory = DepartmentDirectory(gk.state_snapshot())
        gp = directory.for_department("全科门诊")
        self.assertEqual(len(gp), 1)
        self.assertEqual(gp[0]["stage"], "ACTIVE")
        self.assertTrue(gp[0]["physician_signoff_required"])

        ped = directory.for_department("儿科急诊")
        self.assertEqual(ped[0]["stage"], "CANARY")
        self.assertEqual(ped[0]["canary_scope"]["department"], "儿科急诊")
        self.assertTrue(ped[0]["human_backup_required"])  # 逐例复核 → 必须人工兜底
        self.assertEqual(ped[0]["reviewer_role"], "儿科急诊主治")

    def test_directory_omits_blocked_and_rolled_back(self):
        gk = build_gatekeeper()
        prepare_approved_release(gk)
        schedule_and_canary(gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        gk.block_use(release_id="R1", use_id="U-GP", reasons=["THRESHOLD_MISS"], actor="committee")
        directory = DepartmentDirectory(gk.state_snapshot())
        self.assertEqual(directory.for_department("全科门诊"), [])

    def test_recall_keeps_all_records(self):
        gk = build_gatekeeper()
        prepare_approved_release(gk)
        schedule_and_canary(gk, use_ids=["U-GP"], canary_scope=Scope("全科门诊", "成人"))
        gk.activate_use(release_id="R1", use_id="U-GP", actor="ops")
        gk.record_exposure(release_id="R1", use_id="U-GP", case_ref="CASE-9",
                           recommendation_ref="R-9", signoff_physician_id="dr-a",
                           review_confirmed=True, actor="ward")
        gk.decide_recall(release_id="R1", use_ids=["U-GP"], reason="供应商通告缺陷", actor="committee")
        state = gk.state_snapshot()
        self.assertTrue(state.is_recalled("R1", "U-GP"))
        dossier = gk.release_dossier("R1")
        self.assertTrue(dossier.summary()["recalled"])
        # 召回后暴露证据仍在档案里。
        self.assertEqual([e.case_ref for e in dossier.exposures["U-GP"]], ["CASE-9"])
        # 召回版本禁止新暴露。
        with self.assertRaises(GatekeeperError):
            gk.record_exposure(release_id="R1", use_id="U-GP", case_ref="CASE-10",
                               recommendation_ref="R-10", signoff_physician_id="dr-a",
                               review_confirmed=True, actor="ward")


class EventImmutabilityTest(unittest.TestCase):
    def test_events_append_only_and_replay_identical(self):
        gk = build_gatekeeper()
        prepare_approved_release(gk)
        first = gk.store.replay()
        # 再次读取得到的事件内容与第一次完全一致（无法改写历史）。
        second = gk.store.replay()
        self.assertEqual(first, second)
        versions = [e["version"] for e in first]
        self.assertEqual(versions, list(range(1, len(versions) + 1)))


if __name__ == "__main__":
    unittest.main()
