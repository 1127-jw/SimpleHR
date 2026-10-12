"""状态层：把"处理到哪了"持久化到 JSON 文件。

存两样东西：
1. last_uid —— 增量游标。落盘后停机期间到达的邮件下次启动能补处理。
2. seen_ids —— 已处理邮件的 Message-ID 集合（保留最近 500 条）。
   UID 只解决"哪些是新信"，Message-ID 去重解决"回过的信不再回第二遍"，
   两者互补：重连导致游标回退、或同一封邮件被搬动 UID 时靠它兜底。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_SEEN_KEEP = 500


class StateStore:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._data: dict = {"last_uid": None, "seen_ids": []}
        self.load()

    def load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # 文件损坏则从空状态开始，不阻塞启动
                self._data = {"last_uid": None, "seen_ids": []}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # 原子替换，避免写一半断电留下坏文件

    @property
    def last_uid(self) -> Optional[int]:
        return self._data.get("last_uid")

    def set_last_uid(self, uid: int) -> None:
        self._data["last_uid"] = int(uid)
        self._save()

    def is_seen(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self._data["seen_ids"]

    def mark_seen(self, message_id: str) -> None:
        if not message_id:
            return
        ids = self._data.setdefault("seen_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            self._data["seen_ids"] = ids[-_SEEN_KEEP:]  # 只留最近 N 条
            self._save()  # 仅在新增时才写盘，避免每次重复写相同内容
