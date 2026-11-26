#!/usr/bin/env python3
"""复测调度中间件入口。

用法（在仓库根目录，也支持 python3 -I 隔离模式）：

    python3 retest_cli.py run   <状态目录> <演练.jsonl> [更多.jsonl ...] [-o 回执.jsonl] [--strict]
    python3 retest_cli.py inspect <状态目录> [--lab 机构 | --case case]
    python3 retest_cli.py trace  <状态目录> <证书序号>
    python3 retest_cli.py journal <状态目录>

演练文件路径作为参数传入，状态目录与输入资料分离；服务再次运行时
从状态目录的事件日志与快照恢复已占份额和盲审隔离。
"""

import argparse
import json
import sys
from pathlib import Path

# 隔离模式（python3 -I）不会把脚本目录放进 sys.path，这里显式引导
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from retest_scheduler import FeedRunner, JsonlStore  # noqa: E402
from retest_scheduler.middleware import RetestMiddleware  # noqa: E402
from retest_scheduler.models import EngineError  # noqa: E402
from scenario_reader import iter_messages  # noqa: E402


def _load_supported(path: Path):
    registry = path.parent / "message_types.json"
    if registry.exists():
        return set(json.loads(registry.read_text(encoding="utf-8")))
    return {"lab", "packet", "claim", "submit", "revoke", "restart", "inspect"}


def _iter_file(path: Path):
    return iter_messages(path, _load_supported(path))


def cmd_run(args) -> int:
    with RetestMiddleware(args.state_dir) as mw:
        runner = FeedRunner(mw)
        files = [Path(p) for p in args.files]
        for path in files:
            runner.run_iterable(_iter_file(path), source=path.name)

    out = sys.stdout
    if args.out:
        out = open(args.out, "w", encoding="utf-8")
    try:
        for wrapped in runner.receipts:
            out.write(json.dumps(wrapped, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if args.out:
            out.close()

    errors = runner.errors()
    summary = {
        "processed": len(runner.receipts),
        "accepted": len(runner.receipts) - len(errors),
        "rejected": len(errors),
        "error_codes": sorted({e["error"]["code"] for e in errors}),
        "state_dir": str(Path(args.state_dir).resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
    if args.strict and errors:
        return 2
    return 0


def cmd_inspect(args) -> int:
    with RetestMiddleware(args.state_dir) as mw:
        record = {}
        if args.lab:
            record["lab"] = args.lab
        elif args.case:
            record["case"] = args.case
        view = mw.handle(args.at, "inspect", record)
    print(json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_trace(args) -> int:
    with RetestMiddleware(args.state_dir) as mw:
        view = mw.trace_certificate(args.cert_seq)
    print(json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_journal(args) -> int:
    store = JsonlStore(args.state_dir)
    for record in store.journal_records():
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="机器人能力复测调度中间件")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="投喂一个或多个 JSONL 演练文件")
    p_run.add_argument("state_dir", help="本地状态目录（不存在会创建）")
    p_run.add_argument("files", nargs="+", help="演练 JSONL 文件路径")
    p_run.add_argument("-o", "--out", help="回执输出文件，默认标准输出")
    p_run.add_argument("--strict", action="store_true", help="有规则拒绝时退出码为 2")
    p_run.set_defaults(func=cmd_run)

    p_ins = sub.add_parser("inspect", help="按机构或 case 查询状态")
    p_ins.add_argument("state_dir")
    p_ins.add_argument("--lab", help="机构标识；只返回该机构可见的包")
    p_ins.add_argument("--case", help="case 标识；委托方可查项目进度与阻塞")
    p_ins.add_argument("--at", type=int, default=0, help="查询时刻，用于截止清扫")
    p_ins.set_defaults(func=cmd_inspect)

    p_trace = sub.add_parser("trace", help="审计某次出证引用的有效独立决定")
    p_trace.add_argument("state_dir")
    p_trace.add_argument("cert_seq", type=int)
    p_trace.set_defaults(func=cmd_trace)

    p_journal = sub.add_parser("journal", help="导出持久化事件日志")
    p_journal.add_argument("state_dir")
    p_journal.set_defaults(func=cmd_journal)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EngineError as error:
        print(json.dumps({"ok": False, "error": error.to_dict()}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
