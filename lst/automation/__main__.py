r"""自动化接口的命令行：``python -m lst.automation <方法> [JSON 参数] [选项]``。

安装版：

    <安装目录>\runtime\python.exe -I -X utf8 <安装目录>\launcher\worker.py automation <方法> [JSON 参数]

成功时把 result 以缩进的 JSON 打印到标准输出，退出码 0；服务端回复 error 时打印 ``{"error": {...}}``，
退出码 1；连不上或连接中断时打印 ``{"error": {"code": "connection", ...}}``，退出码 2；参数写错时退出码 64。

``storage.restart`` 默认等程序重启完、新进程的接口重新可用后再退出，打印新进程的 status；``--no-wait`` 不等。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import cast

from lst.automation.client import AutomationClient, AutomationError, restart_and_reconnect
from lst.automation.protocol import Json

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONNECTION = 2
EXIT_USAGE = 64


def _print(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="lst.automation", description="调用 LSTtranslate 的自动化接口")
    parser.add_argument("method", help="方法名，例如 status、capture、wait_result、overlay.select")
    parser.add_argument("params", nargs="?", default="{}", help="JSON 对象形式的参数，缺省为 {}")
    parser.add_argument("--pipe", help="管道名，缺省按当前用户生成（或取环境变量 LST_AUTOMATION_PIPE）")
    parser.add_argument("--connect-timeout", type=float, default=5.0, help="连接的等待秒数，缺省 5")
    parser.add_argument(
        "--no-wait", action="store_true", help="storage.restart 发出后立即退出，不等新进程的接口可用"
    )
    parser.add_argument("--restart-timeout", type=float, default=120.0, help="等待重启完成的秒数，缺省 120")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        raw: object = json.loads(args.params)
    except ValueError as exc:
        _print({"error": {"code": "usage", "message": f"参数不是合法的 JSON：{exc}"}})
        return EXIT_USAGE
    if not isinstance(raw, dict):
        _print({"error": {"code": "usage", "message": "参数应当是 JSON 对象"}})
        return EXIT_USAGE
    params = cast(dict[str, Json], raw)
    try:
        client = AutomationClient.connect(args.pipe, timeout=args.connect_timeout)
    except OSError as exc:
        _print({"error": {"code": "connection", "message": str(exc)}})
        return EXIT_CONNECTION
    try:
        if args.method == "storage.restart" and not args.no_wait:
            params.setdefault("confirm", True)
            if params.get("confirm") is not True:
                result: Json = client.call(args.method, params)
            else:
                client = restart_and_reconnect(client, timeout=args.restart_timeout)
                result = {"restarted": True, "status": client.call("status")}
        else:
            result = client.call(args.method, params)
    except AutomationError as exc:
        error: dict[str, Json] = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        _print({"error": error})
        return EXIT_ERROR
    except (OSError, TimeoutError) as exc:
        _print({"error": {"code": "connection", "message": str(exc)}})
        return EXIT_CONNECTION
    finally:
        client.close()
    _print(result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
