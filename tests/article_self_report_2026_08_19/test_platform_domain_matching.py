"""判据⑤ · 平台域清单的匹配面(R2 §① 的地基)。

「平台域清单外连 content_matched 都不给」这条规矩,全靠 `url_is_within_platform_domain`
判得准。它判错的两个方向代价不对称:

  · 判宽(把站外域算成站内)→ 攻击者的镜像站直接拿到线索态,§① 白做;
  · 判窄(把真站内域算成站外)→ 多一条 needs_action,有人多看一眼,可接受。

所以匹配必须**按标签边界**,不能做子串 —— 本仓踩过 `yoojia.com` 含 `jia.com`
子串那一次。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.extension_platform_registry import (  # noqa: E402
    EXTENSION_PLATFORMS,
    platform_domains,
    url_is_within_platform_domain,
)


@pytest.mark.parametrize("platform,url", [
    ("今日头条", "https://www.toutiao.com/article/1/"),
    ("toutiao", "https://mp.toutiao.com/x"),          # 按 id 也要认
    ("知乎", "https://zhuanlan.zhihu.com/p/1"),
    ("知乎", "https://zhihu.com/p/1"),                 # 裸域本身
    ("微信公众号", "https://mp.weixin.qq.com/s/abc"),
    ("B站", "https://www.bilibili.com/read/cv1"),
])
def test_real_platform_urls_are_within(platform, url):
    assert url_is_within_platform_domain(platform, url) is True


@pytest.mark.parametrize("platform,url", [
    # 🔴 子串陷阱:如果实现写成 `d in host`,下面这些会被误判成站内
    ("知乎", "https://evil-zhihu.com/p/1"),
    ("知乎", "https://zhihu.com.attacker.test/p/1"),
    ("今日头条", "https://toutiao.com.evil.test/a"),
    ("今日头条", "https://nottoutiao.com/a"),
    # 完全无关的域
    ("今日头条", "https://attacker-mirror.test/a"),
    # 认不出来的平台 → 空清单 → 一律不在清单内(不能当"随便什么域都行")
    ("某个没登记的平台", "https://anything.test/a"),
    ("", "https://www.toutiao.com/a"),
])
def test_sibling_domain_is_not_within_platform(platform, url):
    assert url_is_within_platform_domain(platform, url) is False


@pytest.mark.parametrize("url", ["", "not a url", "https://", "javascript:alert(1)"])
def test_malformed_urls_are_not_within(url):
    assert url_is_within_platform_domain("今日头条", url) is False


def test_trailing_dot_and_case_are_normalised():
    """`WWW.TOUTIAO.COM.` 与 `www.toutiao.com` 是同一个 host。"""
    assert url_is_within_platform_domain("今日头条", "https://WWW.TOUTIAO.COM./a") is True


def test_every_registered_platform_declares_domains():
    """清单里每个平台都必须有域 —— 少一个就等于那个平台永远拿不到 content_matched。

    这条是**完备性**闸:新加平台忘了填 domains 时当场红,而不是等到线上发现
    那个平台的自报全部落 needs_action。
    """
    missing = [p["name"] for p in EXTENSION_PLATFORMS if not p.get("domains")]
    assert missing == [], f"这些平台没登记域:{missing}"
    for p in EXTENSION_PLATFORMS:
        assert platform_domains(p["name"]) == platform_domains(p["id"]) != ()


def test_registry_is_single_source_shared_with_verifier():
    """单一出处自证:核实器用的就是这一份域判定,不是自己抄了一份。

    [WO_273 · 改指向] 原格核的是插件后端按同名 import 回去的 `EXTENSION_PLATFORMS`;插件后端
    随 WO_273 整体删除,这份清单在役的消费方只剩核实器(`publication_url_verifier.py:84`)。
    守的命题不变 ——「域清单只有一份,两份必然漂移」—— 只是换成核仍在役的那个消费方。
    """
    import services.extension_platform_registry as registry
    import services.publication_url_verifier as verifier

    assert verifier.url_is_within_platform_domain is registry.url_is_within_platform_domain
    assert url_is_within_platform_domain is registry.url_is_within_platform_domain, (
        "分母自证:本文件测的也是同一个对象")
