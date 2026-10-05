"""安全文章路径解析 · 防路径穿越 / 任意本地文件读

[GEO-R1-CAN-016/017/020 P0 修 · 2026-07-12]

旧逻辑漏洞(server.py 原 /api/articles/content 守卫):
    仅当路径含 '..' 且不以 'output/articles' 开头时才拒绝 → 绝对路径
    (如 /proc/self/environ、/etc/passwd、.env)不含 '..' 且不以 output/articles
    开头时【落空放行】→ 任意本地文件读,泄露 DATABASE_URL / JWT 密钥 / 所有 API key。

新逻辑:realpath 规范化(解析符号链接与 ..)+ commonpath 包含校验(fail-closed)。
保持既有合法相对路径('output/articles/...' 相对 cwd)解析行为不变。
越界 / 非法 / 异常 → 返回 None(拒绝),绝不 fallback 放行。

独立成模块(不依赖 server.py / DB),便于纯单元测试。
"""
import os
from typing import Optional


def resolve_safe_article_path(file_path: str, articles_root: Optional[str] = None) -> Optional[str]:
    """把调用方传入的 file_path 规范化为 output/articles 根目录下的真实绝对路径。

    Args:
        file_path: 调用方传入的路径(相对 cwd 的 'output/articles/...' 或绝对路径)
        articles_root: 允许的根(默认 realpath('output/articles'));测试可注入。

    Returns:
        安全的绝对路径(在 articles_root 内);越界/非法/异常返回 None。
    """
    if not file_path or not isinstance(file_path, str):
        return None
    try:
        root = os.path.realpath(articles_root) if articles_root else os.path.realpath("output/articles")
        resolved = os.path.realpath(file_path)
        if resolved == root:
            return resolved
        # commonpath 边界校验:resolved 必须在 root 之下(fail-closed)
        if os.path.commonpath([resolved, root]) == root:
            return resolved
    except (ValueError, OSError):
        # 不同盘符(Windows)/非法路径 → 视为越界
        return None
    return None
