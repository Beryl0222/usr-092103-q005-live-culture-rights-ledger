"""不可变事件存储。

事件只允许追加（append），不提供修改或删除接口。交换格式见
contracts/domain.schema.json：
- 同一 aggregate_id 下 version 从 1 开始严格递增；
- event_id 全局唯一；
- 落库前必须通过 validator 的公共校验；
- 先写内存索引，再以单行 JSON 追加到 JSONL（文件行序即全局先后关系）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .validator import validate_event


class EventStoreError(ValueError):
    """事件违反不可变约定（重复 id、版本断号、载荷非法等）。"""


@dataclass(frozen=True)
class StoredEvent:
    event: dict
    seq: int  # 全局追加序号，从 1 开始，即文件中的行号

    def __getitem__(self, key: str):
        return self.event[key]

    def get(self, key, default=None):
        return self.event.get(key, default)


class EventStore:
    """内存追加日志；JsonlEventStore 在其基础上增加 JSONL 持久化。"""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._ids: set[str] = set()
        self._versions: dict[str, int] = {}

    # ---- 写入 ----
    def append(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        occurred_at: str,
        summary: str,
        payload: dict | None = None,
        event_id: str | None = None,
        version: int | None = None,
    ) -> StoredEvent:
        key = aggregate_id
        next_version = self._versions.get(key, 0) + 1
        if version is not None and version != next_version:
            raise EventStoreError(
                f"聚合 {aggregate_id} 版本必须连续：期望 {next_version}，收到 {version}"
            )

        eid = event_id or f"{aggregate_id}-v{next_version}"
        if eid in self._ids:
            raise EventStoreError(f"event_id 重复：{eid}")

        event = {
            "event_id": eid,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": occurred_at,
            "version": next_version,
            "summary": summary,
        }
        if payload is not None:
            event["payload"] = payload

        errors = validate_event(event)
        if errors:
            raise EventStoreError(f"事件 {eid} 校验失败：{'；'.join(errors)}")

        self._events.append(event)
        self._ids.add(eid)
        self._versions[key] = next_version
        return StoredEvent(event=event, seq=len(self._events))

    def append_raw(self, event: dict) -> StoredEvent:
        """载入历史事件时使用：仍做全部校验，版本须与其在该聚合中的位置一致。"""
        errors = validate_event(event)
        if errors:
            raise EventStoreError(
                f"事件 {event.get('event_id', '?')} 校验失败：{'；'.join(errors)}"
            )
        eid = event["event_id"]
        if eid in self._ids:
            raise EventStoreError(f"event_id 重复：{eid}")
        key = event["aggregate_id"]
        next_version = self._versions.get(key, 0) + 1
        if event["version"] != next_version:
            raise EventStoreError(
                f"聚合 {key} 版本断号：期望 {next_version}，收到 {event['version']}"
            )
        self._events.append(event)
        self._ids.add(eid)
        self._versions[key] = event["version"]
        return StoredEvent(event=event, seq=len(self._events))

    # ---- 读取 ----
    def stream(self):
        for i, event in enumerate(self._events, start=1):
            yield StoredEvent(event=event, seq=i)

    def events_for(self, aggregate_id: str) -> list[StoredEvent]:
        return [
            StoredEvent(event=e, seq=i)
            for i, e in enumerate(self._events, start=1)
            if e["aggregate_id"] == aggregate_id
        ]

    def by_type(self, event_type: str) -> list[StoredEvent]:
        return [
            StoredEvent(event=e, seq=i)
            for i, e in enumerate(self._events, start=1)
            if e["event_type"] == event_type
        ]

    def get(self, event_id: str) -> StoredEvent | None:
        for i, e in enumerate(self._events, start=1):
            if e["event_id"] == event_id:
                return StoredEvent(event=e, seq=i)
        return None

    def __len__(self) -> int:
        return len(self._events)


class JsonlEventStore(EventStore):
    """JSONL 追加持久化。每行一个事件，行序即全局先后关系，历史行不可改写。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            for lineno, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    self.append_raw(json.loads(line))
                except EventStoreError as exc:
                    raise EventStoreError(f"{self.path}:{lineno} 历史日志损坏：{exc}") from exc

    def append(self, **kwargs) -> StoredEvent:
        stored = super().append(**kwargs)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(stored.event, ensure_ascii=False) + "\n")
            f.flush()
        return stored
