"""[P0-2 2026-08-15] 前后端共享平台名单漂移锁。

上一包(6a87b271)取证时发现:后端 `is_shared_platform_domain` 是 host 级精确相等、
前端 `isSharedPlatformDomain` 是后缀匹配,且名单只有 6 项 —— 同一个域在
「共享平台」筛选里和风险标里结论相反。本包把两边统一到同一份**注册域**名单。

名单在两种语言里各存一份是没法避免的,所以把「两份必须逐字相同」做成会转红的事实,
而不是写一句注释指望后人记得(注释传不出去,已经吃过这个亏)。
"""
from __future__ import annotations

import os
import re

from services.media_binding_candidates import (
    B_BUCKET_MAINSTREAM_PORTALS,
    SHARED_PLATFORM_DOMAINS,
)

TSX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "frontend", "src", "pages", "Admin", "GeoPlacementFlywheel.tsx",
)


def _frontend_list() -> list[str]:
    with open(TSX, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    m = re.search(r"const SHARED_PLATFORM_DOMAINS = \[(.*?)\];", text, re.S)
    assert m, "前端找不到 SHARED_PLATFORM_DOMAINS 数组 —— 解析失败时不许静默通过"
    return re.findall(r"'([^']+)'", m.group(1))


def test_parser_actually_found_a_nonempty_list():
    """反向对照:先证明解析器真的解出了东西。空列表会让下面的集合相等恒真。"""
    items = _frontend_list()
    assert len(items) >= 10, f"只解析出 {len(items)} 项,解析器可能没工作"


def test_frontend_and_backend_lists_are_identical():
    assert set(_frontend_list()) == SHARED_PLATFORM_DOMAINS, (
        "前后端共享平台名单漂移了。后端是 SSOT,请把 GeoPlacementFlywheel.tsx 里的数组改成一致。\n"
        f"  只在后端:{sorted(SHARED_PLATFORM_DOMAINS - set(_frontend_list()))}\n"
        f"  只在前端:{sorted(set(_frontend_list()) - SHARED_PLATFORM_DOMAINS)}"
    )


def test_frontend_list_has_no_duplicates():
    items = _frontend_list()
    assert len(items) == len(set(items)), "前端数组有重复项"


def test_frontend_list_excludes_b_bucket():
    """B 桶不许从前端这条路溜进去。"""
    leaked = sorted(set(_frontend_list()) & B_BUCKET_MAINSTREAM_PORTALS)
    assert not leaked, f"B 桶域出现在前端名单里:{leaked}"


def test_frontend_suffix_match_agrees_with_backend_on_sample():
    """前端用后缀匹配、后端用「折注册域后精确相等」。对注册域名单这两者等价 ——
    在关键样本上逐条比对,包括同尾不同域这种最容易出分歧的形态。"""
    from services.media_binding_candidates import is_shared_platform_domain

    front = _frontend_list()

    def front_pred(d: str) -> bool:
        d = d.lower()
        return any(d == h or d.endswith("." + h) for h in front)

    samples = [
        "blog.csdn.net", "c.m.163.com", "news.qq.com", "zhuanlan.zhihu.com",
        "aikahao.xcar.com.cn", "post.m.smzdm.com", "163.com", "qq.com",
        "notcsdn.net", "evilcsdn.net", "fake163.com", "myqq.com",
        "news.cn", "sports.news.cn", "gmw.cn", "dongying.dzwww.com",
        "07358.com", "19lou.com", "agri.example-news.com",
    ]
    mismatch = [d for d in samples if front_pred(d) != is_shared_platform_domain(d)]
    assert not mismatch, f"前后端在这些域上结论不一致:{mismatch}"
