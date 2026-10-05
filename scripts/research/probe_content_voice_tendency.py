# -*- coding: utf-8 -*-
"""真跑 LLM · 量模型**倾向**(不是逻辑)· 2026-08-06

本机固定 stub 测得了逻辑,测不了倾向 —— stub 只会返回我写好的东西,
而我写的东西反映的是"我以为模型会怎么写"。生产 post 19 暴露的正是
我以为的和实际的不一样(品牌名被推到收尾句、brand_line 超 80 字被盲切)。

量四件事,每一件都直接决定一个待定的改法:
  ① `brand_line` / `summary` / `caveat` 的**真实长度分布**  → 上限该定多少
  ② 品牌名落在 body / brand_line / cards 的哪一段          → 要不要收紧"正文必须点名"
  ③ 按现行上限盲切,会不会切掉品牌名                        → 盲切改按句回退的收益
  ④ 新口吻 prompt 的 self_praise 触发率                     → 要不要加自动重生成

🔴 不碰真实客户数据:知识库上下文用**脱敏假客户**,brand_id 传 None,不连生产库。
🔴 不落库、不扣费:只调 LLM,不走 production_task。

用法:
    python scripts/research/probe_content_voice_tendency.py --rounds 4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# ── 脱敏假客户:形状照生产,内容全是编的 ──────────────────────────
FAKE_BRAND = "星野智能（深圳）科技有限公司"
FAKE_CITY = "深圳"
FAKE_INTRO = (
    "星野智能（深圳）科技有限公司成立于2019年，做AI搜索可见度优化，"
    "自研星野引擎，覆盖诊断、内容建设、分发留证、监测复盘全链路。"
    "已服务300+企业，集中在出海电商、职业教育、企业服务三个行业。"
    "白帽打法，不承诺固定排名。"
)
FAKE_USP = "自研诊断 + 素人矩阵 + 闭环监测，效果可验收"


def _fake_ctx():
    from services.geo_douyin.knowledge_context import BrandContext

    ctx = BrandContext(brand_name=FAKE_BRAND)
    for attr, val in (("company_intro", FAKE_INTRO), ("usp", FAKE_USP),
                      ("core_value", FAKE_USP)):
        if hasattr(ctx, attr):
            setattr(ctx, attr, val)
    if hasattr(ctx, "sources_used"):
        ctx.sources_used = ["client_profile"]
    return ctx


async def _one(cg, keyword: str, industry_key: str) -> dict:
    """跑一条,同时抓**截断前**的原文和**截断后**的产出。"""
    raw_box: dict = {}
    orig_call = cg._call_llm

    async def _spy(prompt, **kw):
        out = await orig_call(prompt, **kw)
        raw_box["raw"] = out
        raw_box["prompt_len"] = len(prompt)
        return out

    cg._call_llm = _spy
    try:
        got = await cg.generate_image_post_content(
            keyword, city=FAKE_CITY, brand_name=FAKE_BRAND,
            card_count=4, brand_id=None, industry_key=industry_key)
    finally:
        cg._call_llm = orig_call

    raw = raw_box.get("raw") or ""
    obj = cg._parse_llm_json(raw) or {}
    closing_raw = obj.get("closing") if isinstance(obj.get("closing"), dict) else {}
    body_raw = str(obj.get("body") or "")
    bl_raw = str(closing_raw.get("brand_line") or "")
    sm_raw = str(closing_raw.get("summary") or "")
    cv_raw = str(closing_raw.get("caveat") or "")
    cards_txt = json.dumps(obj.get("cards") or [], ensure_ascii=False)

    from services.geo_douyin.card_templates import CLOSING_TEXT_MAX

    return {
        "ok": got.ok,
        "error": got.error,
        "self_praise": got.self_praise,
        # ① 长度(截断前)
        "len_brand_line": len(bl_raw),
        "len_summary": len(sm_raw),
        "len_caveat": len(cv_raw),
        "len_body": len(body_raw),
        # ② 品牌名落在哪
        "name_in_body": FAKE_BRAND in body_raw,
        "name_in_brand_line": FAKE_BRAND in bl_raw,
        "name_in_cards": FAKE_BRAND in cards_txt,
        "name_in_summary": FAKE_BRAND in sm_raw,
        # ③ 盲切会不会切坏(名字截断前在、截断后没了)
        "cut_kills_name_in_bl": (FAKE_BRAND in bl_raw
                                 and FAKE_BRAND not in bl_raw[:80]),
        "cut_kills_name_in_sm": (FAKE_BRAND in sm_raw
                                 and FAKE_BRAND not in sm_raw[:CLOSING_TEXT_MAX]),
        # ④ 截断点是不是句中(末字不是句读符号 = 半句)
        "bl_cut_midsentence": (len(bl_raw) > 80
                               and bl_raw[79] not in "。；！？，、 "),
        "sm_cut_midsentence": (len(sm_raw) > CLOSING_TEXT_MAX
                               and sm_raw[CLOSING_TEXT_MAX - 1] not in "。；！？，、 "),
        "brand_line_raw": bl_raw,
        "summary_raw": sm_raw,
    }


def _pct(rows, key):
    n = len(rows) or 1
    return 100.0 * sum(1 for r in rows if r.get(key)) / n


def _stat(rows, key):
    vals = [r[key] for r in rows if isinstance(r.get(key), int)]
    if not vals:
        return "n/a"
    return "min=%d 中位=%d max=%d" % (min(vals), int(statistics.median(vals)),
                                      max(vals))


async def main(rounds: int) -> int:
    key = os.environ.get("PROBE_DEEPSEEK_KEY", "").strip()
    if not key:
        print("🔴 没有 PROBE_DEEPSEEK_KEY —— 这个探针必须显式给 key,不走 key 池")
        return 2

    import services.geo_douyin.content_generator as cg
    import services.geo_douyin.knowledge_context as kbc

    # 🔴 必须绕开 key 池:`_get_api_key()` 是**池子优先**,
    #    .env 里的 key 基本永远轮不到 —— 本机拿到的会是池子里那把别的 key,
    #    那等于在花不属于这次授权的额度。这里显式钉死。
    cg._get_api_key = lambda: key
    print("用的 key:", key[:8] + "…(显式指定,已绕开 key 池)")

    async def _ctx(*a, **k):
        return _fake_ctx()

    cg.build_brand_context = _ctx
    kbc.build_brand_context = _ctx

    cases = [
        ("深圳AI搜索优化公司哪家好", "geo_优化服务", "B端"),
        ("深圳全屋定制哪家好", "home_improvement", "C端"),
    ]
    all_rows = []
    for kw, ind, label in cases:
        rows = []
        for i in range(rounds):
            try:
                r = await _one(cg, kw, ind)
            except Exception as e:  # noqa: BLE001
                print("  第 %d 次异常:%s" % (i + 1, str(e)[:120]))
                continue
            r["_seg"] = label
            rows.append(r)
            print("  [%s %d/%d] ok=%s err=%-18s bl=%3d sm=%3d 名字在:%s%s%s"
                  % (label, i + 1, rounds, r["ok"], r["error"] or "-",
                     r["len_brand_line"], r["len_summary"],
                     "正文 " if r["name_in_body"] else "",
                     "收尾句 " if r["name_in_brand_line"] else "",
                     "卡片" if r["name_in_cards"] else ""), flush=True)
        all_rows += rows

        if not rows:
            continue
        print("\n  ── %s 汇总(n=%d)──" % (label, len(rows)))
        print("   brand_line 长度 %s   (现行上限 80)" % _stat(rows, "len_brand_line"))
        print("   summary    长度 %s   (现行上限 80)" % _stat(rows, "len_summary"))
        print("   caveat     长度 %s   (现行上限 40)" % _stat(rows, "len_caveat"))
        print("   正文出现品牌名      : %.0f%%" % _pct(rows, "name_in_body"))
        print("   收尾句出现品牌名    : %.0f%%" % _pct(rows, "name_in_brand_line"))
        print("   卡片出现品牌名      : %.0f%%" % _pct(rows, "name_in_cards"))
        print("   🔴 盲切切掉收尾句品牌名: %.0f%%" % _pct(rows, "cut_kills_name_in_bl"))
        print("   🔴 盲切切掉小结品牌名  : %.0f%%" % _pct(rows, "cut_kills_name_in_sm"))
        print("   🔴 收尾句被切成半句    : %.0f%%" % _pct(rows, "bl_cut_midsentence"))
        print("   🔴 小结被切成半句      : %.0f%%" % _pct(rows, "sm_cut_midsentence"))
        print("   自夸闸触发          : %.0f%%" % _pct(rows, "self_praise"))
        print()

    if all_rows:
        print("══ 全体(n=%d)══" % len(all_rows))
        print("  成功率            : %.0f%%" % _pct(all_rows, "ok"))
        print("  正文点名率        : %.0f%%  ← 决定要不要收紧「正文必须点名」"
              % _pct(all_rows, "name_in_body"))
        print("  brand_line 超 80  : %.0f%%"
              % (100.0 * sum(1 for r in all_rows if r["len_brand_line"] > 80)
                 / len(all_rows)))
        longest = max(all_rows, key=lambda r: r["len_brand_line"])
        print("  最长 brand_line(%d 字):\n    %s" % (longest["len_brand_line"],
                                                    longest["brand_line_raw"]))
        print("  它被盲切成:\n    %s" % longest["brand_line_raw"][:80])
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=4)
    a = p.parse_args()
    raise SystemExit(asyncio.run(main(a.rounds)))
