"""机器人能力复测调度中间件。

协调复测包发放、独立签收与分歧仲裁，仅依赖标准库与本地合成对象。

子模块：
- models：领域对象与错误类型
- engine：派单、盲审、更正、仲裁的全部业务规则
- storage：JSONL 事件日志 + 快照的崩溃恢复
- feed：按演练契约顺序投喂消息
"""

from .engine import RetestEngine
from .models import EngineError
from .storage import JsonlStore
from .feed import FeedRunner

__all__ = ["RetestEngine", "EngineError", "JsonlStore", "FeedRunner"]
