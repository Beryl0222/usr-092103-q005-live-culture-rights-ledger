# 直播文化节目权益账

面向非遗等直播文化节目的**节目权益账服务**：把直播场次、节目段落、参与者、素材来源、
许可用途、地域期限、观众贡献、打赏订单、回放版本与二次剪辑逐段关联。所有授权、发布与
结算变化都以基础仓库约定的**不可变事件**交换，只能追加、不能改写。

## 核心口径

- **事件不可变**：`EventStore` 仅支持追加；`event_id` 全局唯一，同一聚合 `version` 连续
  递增，聚合内时间不允许倒流。纠错只能追加补偿事件（解除限制、退款冲正、补付）。
- **口头同意只是待确认**：直播现场口头同意落 `pending_confirmation`，直播用途临时承认，
  回放/剪辑必须等书面追认（未成年人需 `guardian` 监护人书面同意）。
- **限制到窗口，不删除整场**：未成年人连麦、背景音乐识别不确定、授权到期、权利人要求
  下架等都只遮罩对应的毫秒时段；回放以“可播窗口 + 遮罩窗口及原因”发布。
- **多素材组合取权限交集**：用途（直播/回放/商业剪辑）、地域、时间窗口逐层求交，任一
  来源未覆盖的窗口即不可用，并给出稳定原因码。
- **结算以打赏当时有效合同为准**：合同改版不追溯历史订单；退款整笔冲正；后续确权以
  补付（机构补差）或冲正（资金回归机构）处理，原分配条目永不删除，每行记录合同版本。
- **三类视图各取所需**：编辑看逐段用途矩阵与发布闸门；权利人核对使用范围与逐笔收入；
  观众端只看到已获准内容，被遮罩时段给出“为何暂不可用”的口径化中文说明。

## 资料结构

- `contracts/domain.schema.json`：事件公共信封、26 类事件、11 类聚合与稳定枚举。
- `src/contracts.py`：枚举与事件工厂（用途、同意渠道、限制原因、结算类型）。
- `src/validator.py`：公共信封基础校验。
- `src/event_store.py`：不可变事件存储（追加/版本/时间先后/JSONL 落盘重放）。
- `src/rights_ledger.py`：读模型重放、窗口区间运算、权限交集、派生限制、可用性矩阵、
  回放遮罩计划、剪辑闸门。
- `src/settlement.py`：合同时点快照、首次分配、退款冲正、补付/冲正、权利人对账单。
- `src/views.py`：回放/剪辑发布服务与编辑、权利人、观众三类视图。
- `src/app.py`：命令侧门面（登记场次/段落/参与者/素材/同意/限制/合同/打赏）。
- `scripts/build_sample.py`：一场完整苏绣非遗直播的全链路中文联调样例。
- `data/sample_events.jsonl`：样例生成的 52 条不可变事件。
- `tests/`：契约、存储、权益交集、结算、发布闸门与全链路样例测试。

## 事件与聚合

| 聚合 | 含义 | 关键事件 |
| --- | --- | --- |
| `live_session` | 直播场次 | SESSION_SCHEDULED / STARTED / ENDED |
| `program_segment` | 节目段落（毫秒窗口） | SEGMENT_REGISTERED / SEGMENT_CAST |
| `participant` | 传承人、伴奏、连麦观众（可标记未成年人） | PARTICIPANT_REGISTERED |
| `material_source` | 伴奏曲目、平台切片、老影像等素材 | MATERIAL_REGISTERED / MATERIAL_VERIFIED / MATERIAL_LINKED |
| `usage_permission` | 许可与限制 | CONSENT_CAPTURED / PERMISSION_GRANTED / PERMISSION_EXPIRED / RESTRICTION_APPLIED / RESTRICTION_LIFTED / RELEASE_RESTRICTED |
| `audience_contribution` | 观众/参与者贡献记录 | CONTRIBUTIONS_RECORDED |
| `contract_terms` | 分成合同（按方版本化） | CONTRACT_AGREED |
| `tip_order` | 打赏订单 | TIP_PLACED / TIP_REFUNDED |
| `revenue_entry` | 分配与调整 | REVENUE_ALLOCATED / RIGHTS_ADJUSTED |
| `replay_version` | 回放版本 | REPLAY_PUBLISHED / REPLAY_TAKEN_DOWN |
| `secondary_clip` | 二次剪辑 | CLIP_PROPOSED / CLIP_PUBLISHED / CLIP_REJECTED |

许可用途：`LIVE_WEBCAST`（直播）、`REPLAY`（回放）、`COMMERCIAL_CLIP`（商业剪辑）。
地域 `"*"` 表示全球。限制原因码包括 `minor_in_mic`、`music_unidentified`、
`rights_expired`、`pending_consent`、`declined_consent`、`consent_missing`、
`unlicensed_material`、`use_not_licensed`、`territory_not_licensed`、`takedown_request`。

## 快速上手

```bash
# 重新生成全链路样例事件流
PYTHONPATH=. python3 scripts/build_sample.py

# 运行全部测试
python3 -m unittest discover -s tests
```

典型调用：

```python
from src.app import LedgerService
from src.settlement import SettlementService
from src.views import EditorView, AudienceView, PublishingService
from src import contracts as C

svc = LedgerService()
svc.capture_consent("perm-1", "participant", "p-master", channel=C.CHANNEL_WRITTEN,
                    uses=[C.USE_LIVE, C.USE_REPLAY, C.USE_CLIP])
# 直播中口头同意：自动落 pending_confirmation
svc.capture_consent("perm-2", "participant", "p-fan", channel=C.CHANNEL_ORAL,
                    uses=[C.USE_REPLAY])

# 编辑发布前：逐段看可用于直播/回放/商业剪辑
EditorView(svc.ledger).segment_card("seg-1")

# 结算：按打赏时点合同分配；退款冲正；后续补付
settlement = SettlementService(svc.store, svc.ledger)
settlement.allocate_tip("tip-1001")
settlement.refund_tip("tip-1002", reason="未成年消费退款")
settlement.supplement_payment("tip-1001",
    [{"party": "participant:p-master", "amount_fen": 1500}], reason="后续确权补付")

# 回放只遮罩问题窗口；剪辑必须全程落在权限交集内
PublishingService(svc.store, svc.ledger).publish_replay("live-1", label="回放（遮罩版）")
AudienceView(svc.ledger, territory="CN").replay_feed("live-1")
```

## 联调样例故事线（`data/sample_events.jsonl`）

一场 65 分钟苏绣直播：传承人全程示范；伴奏师仅授直播/回放；成年观众连麦口头同意次日
书面追认；未成年人连麦窗口先限制、监护人同意后解除；背景音乐识别“不确定”→确认并补
曲库授权；一张老影像许可 9-30 到期，发布核对时对应 1 分钟遮罩；权利人要求收尾最后
30 秒下架。打赏两笔：一笔按当时合同首分后补付 15 元并冲正误分 3.5 元；一笔退款整笔
冲正至净额 0。回放版本仅遮罩两个问题窗口；一条合规剪辑发布，一条含伴奏的剪辑被闸门
驳回；观众端只见已获准内容与口径化原因说明。
