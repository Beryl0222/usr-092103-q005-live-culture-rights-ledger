# 直播文化节目权益账

非遗直播里传承人示范、伴奏曲目、观众连麦、平台切片同时出现，节目结束后才核对授权会让
回放下架、打赏分成、短视频传播互相冲突。本仓库提供一套**完整的节目权益账服务参考实现**：
把直播场次、节目段落、参与者、素材来源、许可用途、地域期限、观众贡献、打赏订单、
回放版本与二次剪辑逐段关联；所有授权与发布变化都以基础仓库约定的**不可变事件**保留先后关系。

## 核心规则

- **事件不可变、只追加**：同一聚合 `version` 从 1 严格递增，`event_id` 全局唯一，
  历史行不修改不删除；后续确权只能追加补付（TOP_UP）或冲正（REVERSAL）。
- **口头同意只是待确认记录**：直播中临时口头同意落为 `PENDING`，可覆盖既成的直播播出，
  但回放与商业剪辑必须等补签确认后才放行。
- **限制到时段，不删整场**：未成年人连麦、BGM 识别不确定、权利到期都只在对应毫秒区间
  生效；限制是可解除的事件（监护人补签 / 核实续权后发 `RELEASE_CLEARED`）。
- **多素材权限交集**：同一毫秒出现的每个素材都可用，该毫秒才可用；用途、地域、期限
  全部取交集，任一素材不授权则整毫秒不授权。
- **结算按下单时点有效合同**：打赏即时按当时合同拆分（平台抽成 = 余额），退款追加与原
  分账相反的冲正分录；以后换合同不回溯历史订单，差额靠补付/冲正处理。
- **发布闸门分用途**：回放允许「部分受限 + 播放端按时段屏蔽」；商业剪辑必须整段获准，
  否则驳回并留下 `CUT_REJECTED` 事件。
- **三端同事实、不同细节**：编辑看到直播/回放/商业剪辑三用途的逐片段可用性与完整原因；
  权利人核对使用范围与收入流水；观众端只收到可播区间，未成年人等原因做中性化提示。

## 资料结构

- `contracts/domain.schema.json`：事件公共信封、全部聚合/事件枚举与载荷 $defs。
- `data/sample.json`：最小业务事件样例。
- `data/livestream_scenario.jsonl`：端到端故事线日志（由 `src.demo` 生成，48 个事件）。
- `src/catalogs.py`：聚合、事件、用途、受限原因等稳定枚举与中文文案（含观众端中性文案）。
- `src/validator.py`：事件信封与载荷的公共校验（标准库实现，与 JSON Schema 同义）。
- `src/events.py`：不可变事件存储（内存版 + JSONL 追加持久化，载入即重放校验）。
- `src/policies.py`：毫秒区间运算、许可有效性、多素材权限交集、人工限制叠加等纯规则。
- `src/ledger.py`：事件折叠投影（可按任意历史时点折叠）与 `RightsLedger` 应用服务。
- `src/views.py`：编辑发布台、权利人对账单、观众播放页三类视图。
- `src/demo.py`：一场非遗剪纸直播的完整联调故事线。
- `tests/`：事件不可变性、待确认同意、未成年人/BGM/到期时段限制、权限交集、
  合同时点结算、退款冲正、补付、回放与二剪闸门、三视图一致性。

## 聚合与事件

聚合：`live_session`、`program_segment`、`participant`、`material_source`、
`usage_permission`、`contract`、`viewer_contribution`、`tip_order`、`revenue_entry`、
`replay_version`、`derived_cut`。

事件（节选）：`SEGMENT_REGISTERED`、`MATERIAL_LINKED`、`PERMISSION_GRANTED`、
`CONSENT_CAPTURED → CONSENT_CONFIRMED / CONSENT_WITHDRAWN`、
`RELEASE_RESTRICTED → RELEASE_CLEARED`、`CONTRACT_EXECUTED`、`TIP_PLACED / TIP_REFUNDED`、
`REVENUE_ALLOCATED`、`RIGHTS_ADJUSTED`、`REPLAY_PUBLISHED / REPLAY_TAKEN_DOWN`、
`CUT_PROPOSED / CUT_PUBLISHED / CUT_REJECTED`。

## 故事线速览（2026-10-01 剪纸直播）

1. 演前签订分成合同（传承人 50%、乐手 10%、平台 40%）与正式许可。
2. 直播：示范段全许可；成年观众连麦只有口头同意（待确认）；未成年人连麦自动只限制
   该 10 分钟；谢幕 BGM 指纹识别不确定，先限制对应时段。
3. 打赏按合同 v1 即时结算；次日一笔退款追加反向冲正，原始分录保留。
4. 回放 v1 带两处时段屏蔽上线；含未成年人段的商业剪辑被闸门驳回。
5. 监护人补签、BGM 核实并补授权后，限制按时段解除，切片重新送审通过；乐手少计部分补付。
6. 谢幕伴奏 10-11 到期：观众端仅该 10 分钟变灰，整场仍在；10-15 续约后发回放 v2 恢复。

## 本地检查

```bash
python3 -m unittest discover -s tests     # 全部规则测试
python3 -m src.demo                       # 重建故事线日志并打印三类视图
```

安装 `jsonschema` 后，测试还会用 `contracts/domain.schema.json` 正式校验样例与整条故事线；
未安装时该校验自动跳过。
