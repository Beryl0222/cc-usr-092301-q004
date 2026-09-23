"""测试夹具：可推进的时钟与典型用途/证据工厂。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain import Decision, ReviewMode, RiskClass, Scope
from src.gatekeeper import Gatekeeper
from src.store import EventStore

CST = timezone(timedelta(hours=8))


class Clock:
    """每被读取一次推进一分钟，保证事件时间戳严格有序。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.now_dt = start or datetime(2026, 9, 23, 10, 5, tzinfo=CST)

    def __call__(self) -> datetime:
        value = self.now_dt
        self.now_dt += timedelta(minutes=1)
        return value


def build_gatekeeper(start: datetime | None = None) -> Gatekeeper:
    return Gatekeeper(EventStore(), clock=Clock(start))


def register_gp_purpose(gk: Gatekeeper, **overrides) -> str:
    """基层全科胸片初筛：常规风险，抽查复核。"""
    params = dict(
        use_id="U-GP",
        agent="radio-agent",
        intended_use="基层全科胸片初筛提示",
        risk_class=RiskClass.STANDARD,
        scope=Scope("全科门诊", "成人"),
        key_subgroups=["老年", "非老年"],
        thresholds={"sensitivity": 0.90},
        review_mode=ReviewMode.SPOT_CHECK,
        reviewer_role="全科主治",
        data_purpose="全科胸片筛查质量改进",
        min_sample=200,
        min_subgroup_sample=50,
        actor="registrar",
    )
    params.update(overrides)
    gk.register_purpose(**params)
    return params["use_id"]


def register_ped_purpose(gk: Gatekeeper, **overrides) -> str:
    """儿科急诊危急值提示：高风险，逐例复核。"""
    params = dict(
        use_id="U-PED",
        agent="radio-agent",
        intended_use="儿科急诊影像危急值提示",
        risk_class=RiskClass.HIGH_RISK,
        scope=Scope("儿科急诊", "0-14岁"),
        key_subgroups=["0-3岁", "4-14岁"],
        thresholds={"sensitivity": 0.97},
        review_mode=ReviewMode.REQUIRED,
        reviewer_role="儿科急诊主治",
        data_purpose="儿科急诊影像辅助验证",
        min_sample=500,
        min_subgroup_sample=100,
        actor="registrar",
    )
    params.update(overrides)
    gk.register_purpose(**params)
    return params["use_id"]


def register_two_use_candidate(
    gk: Gatekeeper,
    *,
    release_id: str = "R1",
    model_version: str = "m-2.3.0",
    prompt_version: str = "p-41",
    knowledge_base_version: str = "kb-9",
    dataset_version: str = "ds-2026q3",
) -> str:
    gk.register_version_candidate(
        release_id=release_id,
        use_ids=["U-GP", "U-PED"],
        model_version=model_version,
        prompt_version=prompt_version,
        knowledge_base_version=knowledge_base_version,
        dataset_version=dataset_version,
        change_summary="供应商模型更新与提示词调整",
        actor="vendor",
    )
    return release_id


def passing_subgroups(use_id: str) -> dict:
    if use_id == "U-GP":
        return {
            "老年": {"sample_size": 120, "metrics": {"sensitivity": 0.95}},
            "非老年": {"sample_size": 180, "metrics": {"sensitivity": 0.97}},
        }
    return {
        "0-3岁": {"sample_size": 200, "metrics": {"sensitivity": 0.98}},
        "4-14岁": {"sample_size": 400, "metrics": {"sensitivity": 0.99}},
    }


def submit_passing_validation(gk: Gatekeeper, use_id: str, *, release_id: str = "R1") -> list[str]:
    purpose = gk.state_snapshot().purposes[use_id]
    candidate = gk.state_snapshot().releases[release_id]
    floor = min(purpose["thresholds"].values())
    return gk.submit_validation(
        release_id=release_id,
        use_id=use_id,
        cohort_id=f"cohort-{use_id}",
        dataset_version=candidate["dataset_version"],
        dataset_purpose=purpose["data_purpose"],
        sample_size=max(purpose["min_sample"], 600),
        overall_metrics={"sensitivity": min(0.995, floor + 0.05)},
        subgroup_results=passing_subgroups(use_id),
        leakage_screen_clear=True,
        device_protocol=purpose["scope"].device_protocol,
        submitted_by="analyst-1",
        actor="analyst-1",
    )


def approve(gk: Gatekeeper, use_id: str, *, release_id: str = "R1", approver: str = "dr-chair") -> None:
    gk.committee_decide(
        release_id=release_id,
        use_id=use_id,
        decision=Decision.APPROVE,
        approver_id=approver,
        independent=True,
        rationale=f"{use_id} 证据齐备，批准上线",
        actor=approver,
    )


def schedule_use(gk: Gatekeeper, use_id: str, scope: Scope, *, release_id: str = "R1") -> None:
    """为同一发布中的单个用途单独安排上线窗口（分批放行）。"""
    gk.schedule_rollout(
        release_id=release_id,
        use_ids=[use_id],
        window_start="2026-09-23T10:00:00+08:00",
        window_end="2026-09-30T10:00:00+08:00",
        canary_scope=scope,
        actor="ops",
    )


def canary_use(gk: Gatekeeper, use_id: str, scope: Scope, *, release_id: str = "R1") -> None:
    gk.start_canary(release_id=release_id, use_ids=[use_id], canary_scope=scope, actor="ops")


def schedule_and_canary(
    gk: Gatekeeper,
    *,
    release_id: str = "R1",
    use_ids: list[str] | None = None,
    canary_scope: Scope | None = None,
) -> None:
    use_ids = use_ids or ["U-GP", "U-PED"]
    gk.schedule_rollout(
        release_id=release_id,
        use_ids=use_ids,
        window_start="2026-09-23T10:00:00+08:00",
        window_end="2026-09-30T10:00:00+08:00",
        canary_scope=canary_scope or Scope("全科门诊", "成人"),
        actor="ops",
    )
    gk.start_canary(
        release_id=release_id,
        use_ids=use_ids,
        canary_scope=canary_scope or Scope("全科门诊", "成人"),
        actor="ops",
    )
