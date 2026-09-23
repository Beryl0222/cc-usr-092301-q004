# 临床智能体变更守门

面向医联体多科室使用的临床智能体（智能阅片、病历生成、问诊提示等），
登记用途边界、版本与验证证据，对上线、灰度、漂移与召回做强制守门。

所有状态变化都是**不可变事件**，沿用基线事件信封
`event_id / kind / occurred_at / subject_id / version`，领域负载在
`payload`。委员会可从一次版本发布完整还原「证据 → 审批 → 实际暴露」，
任何结论都可由事件流重放复现。

## 核心模型

- **用途（Use）**：治理最小单元 = 智能体任务 × 科室 × 人群 × 边界。
  登记内容包括任务边界与禁忌、科室与人群、设备/协议范围、风险级别、
  性能阈值与关键亚组阈值、最小验证样本、人工复核职责、授权目的。
- **版本发布（Release）**：模型供应商/模型/提示词/知识库四个版本号
  与一组用途变更的计划。计划冻结（`PLAN_FROZEN`）后版本与范围不可
  再悄悄改动；随后按用途分别登记验证证据。
- **激活（Activation）**：逐用途通过闸门后才允许在线，绑定上线窗口
  与灰度计划（比例上限 + 病例数硬上限）。
- **暴露（Exposure）**：自动建议每影响一个病例都登记：病例、责任医生、
  授权目的、医生签字事实、实际使用的版本。挂起/召回后历史暴露仍保留。

## 强制守门规则

| 规则 | 行为 |
| --- | --- |
| 医生签字不可替代 | `SIGN_OFF_REQUIRED` 用途的每次暴露必须携带医生已签字事实；服务没有任何「代签字」入口，未签字即拒绝 |
| 高风险独立批准 | `HIGH` 用途激活前须有独立审查人批准且声明独立性，同时满足总体与亚组最小样本 |
| 验证阈值 | 总体指标、关键亚组指标分别对照各自阈值；亚组样本不足或退化单独阻断 |
| 数据泄漏 | 验证队列发现泄漏只阻断该用途；同发布/同科室其他用途不受影响 |
| 设备协议变化 | 登记的协议集合未被验证覆盖即阻断；协议升级须先「修订用途边界」，再在新发布中重新验证 |
| 阻断半径 | 泄漏、协议不匹配、亚组退化、真实漂移、不良事件、召回全部以 `use_id` 为作用域 |
| 上线窗口 | 只在 `[opens_at, closes_at)` 内允许激活 |
| 灰度范围 | 计划放量比例不得超过灰度上限；每例暴露必须携带分流比例，超比例拒绝；病例数到硬上限拒绝 |
| 漂移分诊 | 信号必须分诊为数据延迟 / 工作流变化 / 真实退化；前两类不阻断，真实退化自动挂起受影响用途 |
| 挂起恢复 | 挂起后不能凭旧发布恢复：必须在挂起之后的新发布里重新验证、重新批准 |
| 召回 | 委员会召回后该用途永久下线、同版本不得再激活；不得借「修订边界」复活，须按新用途重新登记 |
| 不良事件 | 哨事件自动预防性挂起，等待委员会决定 |
| 目的限制 | 暴露的 `purpose` 必须在用途登记的授权目的内；越权尝试被拒绝**并留痕** |

## 代码结构

- `src/domain.py`：领域类型（用途、阈值、亚组、复核职责、发布、灰度、
  漂移原因）与全部拒绝原因码 `Violation`。
- `src/events.py`：不可变事件、事件存储与事件流 → 只读快照的折叠逻辑。
- `src/gatekeeper.py`：守门服务。登记/计划/冻结/证据/批准/激活/暴露/
  漂移分诊/挂起/召回/不良事件，以及委员会卷宗、科室指南、受影响病例
  三个只读视图。
- `src/contract.py`、`contracts/event.schema.json`：基线事件信封合同。

## 典型流程

```python
gk.register_use(UseScope(use_id="cxr-ped-ed", department="儿科急诊",
                         risk_tier=RiskTier.HIGH,
                         review=ReviewResponsibility(mode=ReviewMode.SIGN_OFF_REQUIRED, ...),
                         thresholds=(Threshold("sensitivity", 0.95),),
                         subgroups=(SubgroupSpec("under_3", Threshold("sensitivity", 0.93), 100),),
                         minimum_sample=500, device_protocols=("DR-A/v2", "DR-B/v2")))
gk.plan_release("R-2026-09", model=ModelVersion(...), uses=[PlannedUse(...)], window=...)
gk.freeze_plan("R-2026-09", by="秘书")
gk.record_validation("R-2026-09", "cxr-ped-ed", cohort_id=..., overall=[...], subgroups={...})
gk.grant_independent_approval("R-2026-09", "cxr-ped-ed", independence_declared=True, ...)
gk.activate("R-2026-09", "cxr-ped-ed", by="发布经理")
gk.record_exposure("cxr-ped-ed", case_ref="CASE-1", physician_id="dr-sun",
                   purpose="clinical_decision_support", signed_off=True, fraction_bucket=0.10)
```

查询：

- `release_dossier(release_id)`：一次发布的计划、每用途证据、独立批准、
  激活、阻断史、暴露病例清单。
- `department_guide("儿科急诊")`：当前允许使用的用途、灰度范围、版本、
  何时必须人工兜底，以及被挂起/召回的用途与原因。
- `affected_cases(use_id=..., release_id=...)`：回滚后仍可查曾影响的病例。

## 本地检查

```bash
python -m unittest discover -s tests
```

`tests/test_gatekeeper.py` 以「儿科急诊胸片初筛（高风险、3 岁以下亚组、
双设备协议、10% 灰度）」与「基层全科问诊分诊（常规风险）」两个用途贯穿
全部守门规则。
