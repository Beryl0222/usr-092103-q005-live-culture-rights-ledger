"""不可变事件存储。

唯一允许的写操作是追加：event_id 全局唯一、同一聚合的 version 必须连续递增、
聚合内 occurred_at 不允许倒流。任何修改既有事件的做法都会抛错；
纠错只能通过追加补偿事件（冲正/补付/解除）完成。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .contracts import ContractError
from .validator import validate_event


class EventStore:
    def __init__(self) -> None:
        self._events: list[dict] = []
        self._ids: set[str] = set()
        self._versions: dict[tuple[str, str], int] = {}
        self._last_at: dict[tuple[str, str], str] = {}

    def append(self, event: dict) -> None:
        errors = validate_event(event)
        if errors:
            raise ContractError("；".join(errors))
        event_id = event["event_id"]
        if event_id in self._ids:
            raise ContractError(f"event_id 重复，事件不可重复写入：{event_id}")
        key = (event["aggregate_type"], event["aggregate_id"])
        expected = self._versions.get(key, 0) + 1
        if event["version"] != expected:
            raise ContractError(
                f"聚合 {event['aggregate_type']}:{event['aggregate_id']} version 必须为 {expected}，"
                f"收到 {event['version']}（不允许跳号或改写）"
            )
        last_at = self._last_at.get(key)
        if last_at is not None and event["occurred_at"] < last_at:
            raise ContractError(
                f"聚合 {event['aggregate_id']} 事件时间倒流：{event['occurred_at']} 早于 {last_at}"
            )
        self._events.append(event)
        self._ids.add(event_id)
        self._versions[key] = event["version"]
        self._last_at[key] = event["occurred_at"]

    def next_version(self, aggregate_type: str, aggregate_id: str) -> int:
        """该聚合下一条事件应使用的 version。"""
        return self._versions.get((aggregate_type, aggregate_id), 0) + 1

    def events(self) -> list[dict]:
        """按追加顺序返回事件的只读快照。"""
        return [dict(e) for e in self._events]

    def by_aggregate(self, aggregate_type: str, aggregate_id: str) -> list[dict]:
        return [dict(e) for e in self._events
                if e["aggregate_type"] == aggregate_type and e["aggregate_id"] == aggregate_id]

    def save_jsonl(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for event in self._events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    @classmethod
    def load_jsonl(cls, path: str | Path) -> "EventStore":
        store = cls()
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                store.append(json.loads(line))
        return store

    def __len__(self) -> int:
        return len(self._events)


def parse_ts(value: str) -> datetime:
    """解析事件时间字符串；无时区按 +08:00（直播排期本地口径）处理。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt
