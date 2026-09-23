"""验证闸门策略——把证据与用途边界逐条比对。

闸门的结论只针对一个 (release, use)，因此任何失败的阻断半径都天然限定在
相关用途：泄漏、设备协议变化、亚组退化都只阻断对应 scope 的用途，
不会波及同一版本下其他已证明安全的场景。
"""

from __future__ import annotations

from .domain import GateFailure
from .state import ValidationEvidence


def evaluate_validation(purpose: dict, candidate: dict, evidence: ValidationEvidence) -> list[str]:
    """返回失败原因代码列表；空列表表示闸门通过。"""
    reasons: list[str] = []

    # 1) 数据必须在原授权目的内使用——患者数据不得跨目的复用。
    if evidence.dataset_purpose != purpose["data_purpose"]:
        reasons.append(GateFailure.PURPOSE_MISMATCH)

    # 2) 证据所用数据版本必须与候选版本登记的一致，防止旧证据冒充新验证。
    if evidence.dataset_version != candidate["dataset_version"]:
        reasons.append(GateFailure.DATASET_VERSION_MISMATCH)

    # 3) 泄漏筛查未通过 → 阻断该用途。
    if not evidence.leakage_screen_clear:
        reasons.append(GateFailure.LEAKAGE)

    # 4) 设备协议必须与用途登记一致（协议变化需重新验证，不得静默沿用）。
    purpose_protocol = purpose["scope"].device_protocol
    if evidence.device_protocol != purpose_protocol:
        reasons.append(GateFailure.DEVICE_PROTOCOL)

    # 5) 最小样本量（高风险用途由登记时设更高的下限）。
    if evidence.sample_size < purpose["min_sample"]:
        reasons.append(GateFailure.SAMPLE_TOO_SMALL)

    # 6) 总体指标必须逐项达到登记阈值。阈值形如 {"sensitivity": 0.95}。
    for metric, floor in purpose["thresholds"].items():
        value = evidence.overall_metrics.get(metric)
        if value is None or value < floor:
            reasons.append(GateFailure.THRESHOLD_MISS)
            break

    # 7) 每个关键亚组都必须有结果、达到亚组最小样本与阈值。
    subgroup_results = evidence.subgroup_results or {}
    floors = purpose.get("subgroup_thresholds") or purpose["thresholds"]
    min_sub = purpose["min_subgroup_sample"]
    for subgroup in purpose["key_subgroups"]:
        result = subgroup_results.get(subgroup)
        if not result:
            reasons.append(GateFailure.MISSING_SUBGROUP)
            continue
        if result.get("sample_size", 0) < min_sub:
            reasons.append(GateFailure.SAMPLE_TOO_SMALL)
        for metric, floor in floors.items():
            value = result.get("metrics", {}).get(metric)
            if value is None or value < floor:
                reasons.append(GateFailure.SUBGROUP_DEGRADATION)
                break

    return reasons
