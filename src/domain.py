"""领域词表：用途边界、风险分级、放行阶段、漂移分类与人工兜底策略。

这些枚举是委员会、科室与供应商之间的共同语言；持久化只存其字符串值。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RiskClass(StrEnum):
    """用途风险分级。高风险用途强制独立批准与最小样本要求。"""

    STANDARD = "STANDARD"          # 常规用途
    HIGH_RISK = "HIGH_RISK"        # 高风险用途（如儿科急诊辅助决策）


class Stage(StrEnum):
    """单个用途在某一版本下的放行阶段，只能逐级推进。"""

    CANDIDATE = "CANDIDATE"        # 已登记候选，尚无证据
    VALIDATED = "VALIDATED"        # 验证闸门通过
    APPROVED = "APPROVED"          # 临床安全委员会批准
    SCHEDULED = "SCHEDULED"        # 已排入上线窗口
    CANARY = "CANARY"              # 灰度中
    ACTIVE = "ACTIVE"              # 全量上线
    HUMAN_BACKUP = "HUMAN_BACKUP"  # 在线但强制人工复核兜底
    BLOCKED = "BLOCKED"            # 证据失效，阻断该用途
    ROLLED_BACK = "ROLLED_BACK"    # 已回滚（影响记录仍保留）
    SUPERSEDED = "SUPERSEDED"      # 已被同用途的更新全量版本取代（历史仍可查）


class DriftCategory(StrEnum):
    """线上漂移信号三分类——只有真实性能下降才允许触发阻断。"""

    DATA_DELAY = "DATA_DELAY"              # 数据延迟，指标假退化
    WORKFLOW_CHANGE = "WORKFLOW_CHANGE"    # 工作流变化导致的口径变化
    REAL_DEGRADATION = "REAL_DEGRADATION"  # 真实性能下降


class GateFailure(StrEnum):
    """验证闸门失败原因。每种原因的阻断半径都被限定在相关用途内。"""

    LEAKAGE = "LEAKAGE"                # 验证数据泄漏
    DEVICE_PROTOCOL = "DEVICE_PROTOCOL"  # 设备协议变化
    SUBGROUP_DEGRADATION = "SUBGROUP_DEGRADATION"  # 关键亚组指标退化
    THRESHOLD_MISS = "THRESHOLD_MISS"  # 总体指标未达阈值
    SAMPLE_TOO_SMALL = "SAMPLE_TOO_SMALL"  # 样本量不足
    PURPOSE_MISMATCH = "PURPOSE_MISMATCH"  # 数据原授权目的不一致
    DATASET_VERSION_MISMATCH = "DATASET_VERSION_MISMATCH"  # 验证数据版本与候选登记不一致
    MISSING_SUBGROUP = "MISSING_SUBGROUP"  # 关键亚组无验证结果


class ReviewMode(StrEnum):
    """人工复核职责。任何自动建议都不能替代医生签字。"""

    REQUIRED = "REQUIRED"      # 每条建议必须医生签字
    SPOT_CHECK = "SPOT_CHECK"  # 按比例抽查 + 异常必审
    ON_DEMAND = "ON_DEMAND"    # 医生随时可调取，默认不阻断


class Decision(StrEnum):
    """委员会/守门人的明确决定。"""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    RECALL = "RECALL"


@dataclass(frozen=True)
class Scope:
    """适用范围：科室 + 人群 + 设备协议。

    阻断与放行的最小单位是 Scope 而不是整版模型——儿科急诊出问题
    不能连累已经证明安全的基层全科。
    """

    department: str
    population: str
    device_protocol: str = "DEFAULT"

    def key(self) -> str:
        return f"{self.department}|{self.population}|{self.device_protocol}"

    def to_dict(self) -> dict:
        return {
            "department": self.department,
            "population": self.population,
            "device_protocol": self.device_protocol,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Scope":
        return cls(
            department=data["department"],
            population=data["population"],
            device_protocol=data.get("device_protocol", "DEFAULT"),
        )
