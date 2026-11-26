"""崩溃恢复存储：事件先落盘并 fsync，再作用于内存；重启靠重放重建。"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .engine import EVENT_TYPES, RetestEngine, State


class JsonlStore:
    """单目录本地存储，只含标准库。

    - journal.jsonl：只追加的事件日志，每行 {"seq", "type", "payload", "at"}，
      seq 单调连续，是状态的唯一事实源
    - snapshot.json：原子替换的状态快照与重放位点，仅用于加速重放
    """

    JOURNAL = "journal.jsonl"
    SNAPSHOT = "snapshot.json"

    def __init__(self, directory: str, snapshot_interval: int = 25):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.directory / self.JOURNAL
        self.snapshot_path = self.directory / self.SNAPSHOT
        self.snapshot_interval = snapshot_interval
        self.last_seq = 0
        self.events_since_snapshot = 0
        self._fh = None
        self._engine: Optional[RetestEngine] = None

    def open(self) -> RetestEngine:
        """从事件日志完整重放重建；快照仅在日志被轮转后作为加速缓存。

        事实源始终是只追加的 journal.jsonl：服务异常退出后，
        已 fsync 的事件一条不少，重放结果与崩溃前严格一致。
        """
        state, snapshot_seq, _ = self._load_snapshot()
        engine = RetestEngine(state)
        self._engine = engine
        self.last_seq = 0
        self.events_since_snapshot = 0
        if self.journal_path.exists():
            with self.journal_path.open("r", encoding="utf-8") as source:
                for line_number, raw in enumerate(source, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                        event_type = record["type"]
                        payload = record["payload"]
                        seq = record["seq"]
                    except (json.JSONDecodeError, KeyError, TypeError) as error:
                        raise RuntimeError(
                            f"事件日志第 {line_number} 行损坏：{error}"
                        ) from error
                    if event_type not in EVENT_TYPES:
                        raise RuntimeError(f"事件日志含未知事件：{event_type}")
                    if seq != self.last_seq + 1:
                        raise RuntimeError(
                            f"事件序号不连续：期望 {self.last_seq + 1}，实际 {seq}"
                        )
                    if seq <= snapshot_seq:
                        # 已在快照中覆盖，保留审计记录但不重复 apply
                        self.last_seq = seq
                        continue
                    engine.apply_event(event_type, payload)
                    self.last_seq = seq
                    self.events_since_snapshot += 1
        self._fh = self.journal_path.open("a", encoding="utf-8")
        return engine

    def _load_snapshot(self) -> Tuple[State, int, int]:
        if not self.snapshot_path.exists():
            return State(), 0, 0
        data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        state = State.from_dict(data["state"])
        return state, data["last_seq"], 0

    def append(self, event_type: str, payload: Dict[str, Any], at: int) -> int:
        if self._fh is None:
            raise RuntimeError("存储未 open")
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未登记的事件类型：{event_type}")
        self.last_seq += 1
        record = {"seq": self.last_seq, "type": event_type, "payload": payload, "at": at}
        self._fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        # 进程异常退出也必须保留已占份额：每行强制刷盘
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.events_since_snapshot += 1
        if self.events_since_snapshot >= self.snapshot_interval:
            self.snapshot()
        return self.last_seq

    def snapshot(self, engine: Optional[RetestEngine] = None) -> None:
        engine = engine or self._engine
        if engine is None:
            raise RuntimeError("快照需要引擎实例")
        tmp = self.snapshot_path.with_suffix(".tmp")
        body = json.dumps(
            {
                "last_seq": self.last_seq,
                "events_since_snapshot": 0,
                "state": engine.state.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self.snapshot_path)
        # 快照目录项也需要落盘，保证 rename 在断电后可见
        dir_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        self.events_since_snapshot = 0

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None

    def journal_records(self) -> List[Dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
