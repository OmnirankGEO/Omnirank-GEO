"""统计 frontend/playwright.*.config.* 里 `webServer.command` 走 npm/npx 壳的份数。

🔴 为什么单独写脚本而不是一条 grep:R3-P4 上抛用的是
``grep 'command:' | grep npm`` 的**行数**,那是"所有 command 行"而不是
"webServer 块里的 command",口径不同、数也不同。数字要能被复算,
就得把口径写成代码。

用法:python scripts/census_playwright_webserver_shell.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_FRONTEND = Path("frontend")
_SHELL = re.compile(r"(^|[\s&|;])(npm|npx|yarn|pnpm)\b")


def _balanced(source: str, start: int) -> str:
    """从 start 处的 `{` 或 `[` 起做括号配对,返回整段。"""
    opening = source[start]
    closing = {"{": "}", "[": "]"}[opening]
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    return source[start:]


def webserver_block(source: str) -> str | None:
    """取 `webServer:` 属性的值(对象或**数组** —— 有 3 份配置是数组形式)。"""
    match = re.search(r"webServer\s*:\s*[\{\[]", source)
    if not match:
        return None
    return _balanced(source, match.end() - 1)


def main() -> int:
    files = sorted(_FRONTEND.glob("playwright.*.config.*"))
    with_server: list[Path] = []
    shell: list[tuple[str, str]] = []
    unparsed: list[str] = []
    for path in files:
        source = path.read_text(encoding="utf-8", errors="replace")
        block = webserver_block(source)
        if block is None:
            continue
        with_server.append(path)
        commands = re.findall(r"command\s*:\s*['\"`]([^'\"`]+)", block)
        if not commands:
            unparsed.append(path.name)
            continue
        for command in commands:
            if _SHELL.search(command):
                shell.append((path.name, command))
                break

    print("配置文件总数            :", len(files))
    print("其中带 webServer 的      :", len(with_server))
    print("webServer.command 走壳的 :", len(shell))
    for name, command in shell:
        print("   ", name, "->", command)
    if unparsed:
        print("带 webServer 但解析不出 command:", unparsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
