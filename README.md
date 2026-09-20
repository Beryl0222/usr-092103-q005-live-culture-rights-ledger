# 直播文化节目权益账

本仓库保存直播文化节目权益账的领域词汇、交换事件与中文联调样例，供后续服务在统一身份和版本语义下协作。

## 资料结构

- `contracts/domain.schema.json`：领域事件的公共信封与稳定枚举。
- `data/sample.json`：一条最小业务事件样例。
- `src/`：公共字段的基础校验代码。
- `tests/`：验证样例能够通过基础约定。

当前核心对象包括live_session、program_segment、usage_permission、revenue_entry，已登记的事件类型为SEGMENT_REGISTERED、CONSENT_CAPTURED、RELEASE_RESTRICTED、REVENUE_ALLOCATED、RIGHTS_ADJUSTED。这些内容只规定跨模块交换的起点，不包含具体业务流程、存储或接口实现。

## 本地检查

```bash
python3 -m unittest discover -s tests
```
