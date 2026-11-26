"""中间件门面：契约消息 -> 引擎命令；事件先持久化再作用于内存。"""

from typing import Any, Dict, List, Tuple

from .engine import RetestEngine
from .models import EngineError
from .storage import JsonlStore


class RetestMiddleware:
    """可重入的复测调度中间件；每次运行从同一目录完整恢复状态。"""

    def __init__(self, directory: str, snapshot_interval: int = 25):
        self.store = JsonlStore(directory, snapshot_interval=snapshot_interval)
        self.engine: RetestEngine = self.store.open()

    # ------------------------------------------------------------------
    def _commit(self, events: List[Tuple[str, Dict[str, Any]]], at: int) -> List[int]:
        """先追加并 fsync 事件日志，全部落盘成功后才在内存生效。

        若中途写盘失败，内存状态尚未改变，下一次启动按日志重放，
        已占份额与盲审隔离不会因进程异常退出而丢失或重复。
        """
        seqs = []
        for event_type, payload in events:
            seqs.append(self.store.append(event_type, payload, at))
        for event_type, payload in events:
            self.engine.apply_event(event_type, payload)
        return seqs

    def handle(self, at: int, kind: str, record: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            raise EngineError("unknown_kind", f"未登记的消息名称：{kind}")
        return handler(at, record)

    # ------------------------------------------------------------------
    def _on_lab(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        events, receipt = self.engine.register_lab(
            at,
            record["lab"],
            record["affinity"],
            list(record["skills"]),
            int(record["slots"]),
            bool(record.get("active", True)),
        )
        seqs = self._commit(events, at)
        receipt["persisted_events"] = len(seqs)
        return receipt

    def _on_packet(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        key = (record["case"], record["item"])
        if key not in self.engine.state.packets:
            events, receipt = self.engine.publish_packet(
                at,
                record["case"],
                record["item"],
                record["skill"],
                int(record["evidence"]),
                int(record["deadline"]),
            )
        else:
            events, receipt = self.engine.revise_evidence(
                at,
                record["case"],
                record["item"],
                int(record["evidence"]),
                int(record["deadline"]),
            )
        self._commit(events, at)
        receipt["persisted_events"] = len(events)
        return receipt

    def _on_claim(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        events, receipt = self.engine.claim(
            at,
            record["lab"],
            record["case"],
            record["item"],
            record["role"],
            record["request"],
        )
        if events:  # 重复申请不产生新事件、不占新份额
            self._commit(events, at)
            receipt["persisted_events"] = len(events)
        return receipt

    def _on_submit(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        events, receipt = self.engine.submit_result(
            at,
            record["lab"],
            int(record["assignment"]),
            int(record["evidence"]),
            record["verdict"],
            list(record.get("refs", [])),
        )
        self._commit(events, at)
        receipt["persisted_events"] = len(events)
        return receipt

    def _on_revoke(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        events, receipt = self.engine.revoke_assignment(
            at, int(record["assignment"]), record.get("reason", "")
        )
        self._commit(events, at)
        receipt["persisted_events"] = len(events)
        return receipt

    def _on_restart(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        events, receipt = self.engine.record_restart(at)
        self._commit(events, at)
        # 重启点立即落快照，缩短下次重放链路
        self.store.snapshot(self.engine)
        return receipt

    def _on_inspect(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        # 查询时顺带把已截止任务落盘为过期，保证视图与可领取状态一致
        sweep_events = self.engine.pending_sweep_events(at)
        if sweep_events:
            self._commit(sweep_events, at)
        if "lab" in record:
            return self.engine.inspect_lab(record["lab"])
        if "case" in record:
            return self.engine.inspect_case(record["case"])
        return self.engine.inspect_overview()

    # ------------------------------------------------------------------
    def trace_certificate(self, cert_seq: int) -> Dict[str, Any]:
        return self.engine.trace_certificate(cert_seq)

    def snapshot(self) -> None:
        self.store.snapshot(self.engine)

    def close(self) -> None:
        self.store.snapshot(self.engine)
        self.store.close()

    def __enter__(self) -> "RetestMiddleware":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.store.close()
