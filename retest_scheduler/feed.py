"""按演练契约顺序投喂 JSONL 消息，收集回执与被拒绝的消息。"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

# 隔离模式下保证能找到同仓库的演练读取器与中间件包
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from .middleware import RetestMiddleware
from .models import EngineError


class FeedRunner:
    """逐条处理消息；规则拒绝不中断整段演练，而是记录 error 回执。"""

    def __init__(self, middleware: RetestMiddleware):
        self.mw = middleware
        self.receipts: List[Dict[str, Any]] = []

    def run_file(self, path: str) -> List[Dict[str, Any]]:
        from scenario_reader import iter_messages  # 本地演练读取器

        supported = set(
            json.loads(
                (Path(path).parent / "message_types.json").read_text(encoding="utf-8")
            )
        )
        return self.run_iterable(iter_messages(Path(path), supported), source=Path(path).name)

    def run_iterable(
        self, messages: Iterable[Dict[str, Any]], source: str = "stream"
    ) -> List[Dict[str, Any]]:
        for index, message in enumerate(messages, 1):
            at, kind, record = message["at"], message["kind"], message["record"]
            try:
                receipt = self.mw.handle(at, kind, record)
                wrapped = {
                    "source": source,
                    "line": index,
                    "at": at,
                    "kind": kind,
                    "ok": True,
                    "receipt": receipt,
                }
            except EngineError as error:
                wrapped = {
                    "source": source,
                    "line": index,
                    "at": at,
                    "kind": kind,
                    "ok": False,
                    "error": error.to_dict(),
                    "record": record,
                }
            self.receipts.append(wrapped)
        return self.receipts

    def errors(self) -> List[Dict[str, Any]]:
        return [r for r in self.receipts if not r["ok"]]
