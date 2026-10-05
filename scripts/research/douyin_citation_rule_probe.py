"""§6c · 反推豆包的"引用规则":被采纳 vs 仅被检索,差在哪

方法:**配对分析**。同一条 prompt 下,豆包检索到了 N 条抖音,只采纳了其中一部分。
两组共享同一个查询意图,因此组间差异 ≈ 采纳偏好本身(而不是 query 难度差异)。

测的四个候选规则:
  R1 检索位次决定采纳?   → 比 source_position
  R2 标题是索引?         → 比 标题↔查询词 的词面重合度
  R3 标题长度/信息量?     → 比 标题字数
  R4 形态(视频/图文)?     → 需 TIKHUB,单独跑

输入 = ro_citation_rule.sh 第 5 段的 TSV(signal_tier / source_position / prompt / title / url)。
用法:  python scripts/research/douyin_citation_rule_probe.py --tsv .tmp_ro/paired.tsv
"""
from __future__ import annotations

import argparse
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Dict, List

# 查询词里要剔除的功能词(留下实义词做重合度)
_STOP = set("的了吗呢啊哪几家哪家好推荐一下请问我想知道有哪些什么怎么样如何"
            "以内左右附近这个那个可以比较适合大家帮我介绍谁是哪个哪些")
_PUNCT = re.compile(r"[^一-龥a-zA-Z0-9]+")


def terms(text: str) -> set:
    """中文按 2-gram 取词面单元(无分词依赖),英文/数字按 token。

    用 2-gram 而不是整句包含:标题极少原样含整个问句,但会大量命中「全屋定制」
    「哪家好」这类 2-4 字片段 —— 2-gram 能量化这种局部重合。
    """
    s = _PUNCT.sub("", text or "")
    out = set()
    for i in range(len(s) - 1):
        g = s[i:i + 2]
        if g not in _STOP:
            out.add(g)
    return out


def overlap(prompt: str, title: str) -> float:
    """标题覆盖了查询词多少比例的 2-gram(0-1)。"""
    p, t = terms(prompt), terms(title)
    if not p:
        return 0.0
    return len(p & t) / len(p)


def parse(tsv: Path) -> List[Dict]:
    rows: List[Dict] = []
    for line in tsv.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 5 or parts[0] not in ("answer_adopted", "search_result_only"):
            continue
        try:
            pos = int(parts[1])
        except ValueError:
            pos = -1
        rows.append({"tier": parts[0], "pos": pos, "prompt": parts[2],
                     "title": parts[3], "url": parts[4]})
    return rows


def _stat(vals: List[float]) -> str:
    if not vals:
        return "n=0"
    return (f"n={len(vals)} 均值={statistics.mean(vals):.3f} "
            f"中位={statistics.median(vals):.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tsv", required=True)
    args = ap.parse_args()

    rows = parse(Path(args.tsv))
    ad = [r for r in rows if r["tier"] == "answer_adopted"]
    so = [r for r in rows if r["tier"] == "search_result_only"]
    print(f"配对样本:被采纳 {len(ad)} / 仅检索 {len(so)}\n")

    print("== R1 检索位次是否决定采纳 ==")
    pa = [r["pos"] for r in ad if r["pos"] >= 0]
    ps = [r["pos"] for r in so if r["pos"] >= 0]
    print("  被采纳 ", _stat([float(x) for x in pa]))
    print("  仅检索 ", _stat([float(x) for x in ps]))

    print("\n== R2 标题↔查询词 词面重合度 ==")
    oa = [overlap(r["prompt"], r["title"]) for r in ad]
    os_ = [overlap(r["prompt"], r["title"]) for r in so]
    print("  被采纳 ", _stat(oa))
    print("  仅检索 ", _stat(os_))
    if oa and os_:
        lift = (statistics.mean(oa) / statistics.mean(os_) - 1) * 100
        print(f"  → 被采纳组重合度比仅检索组高 {lift:+.1f}%")

    print("\n== R3 标题长度 ==")
    la = [float(len(r["title"])) for r in ad]
    ls = [float(len(r["title"])) for r in so]
    print("  被采纳 ", _stat(la))
    print("  仅检索 ", _stat(ls))

    print("\n== R3b 标题是否含 hashtag(#) ==")
    for name, g in (("被采纳", ad), ("仅检索", so)):
        n = sum(1 for r in g if "#" in r["title"])
        print(f"  {name} 含#: {n}/{len(g)} = {n*100//max(1,len(g))}%")

    print("\n== R3c 标题是否含数字 ==")
    for name, g in (("被采纳", ad), ("仅检索", so)):
        n = sum(1 for r in g if re.search(r"\d", r["title"]))
        print(f"  {name} 含数字: {n}/{len(g)} = {n*100//max(1,len(g))}%")

    print("\n== 组内配对:同一 prompt 下,被采纳的重合度是否更高 ==")
    byp: Dict[str, Dict[str, List[float]]] = {}
    for r in rows:
        byp.setdefault(r["prompt"], {"a": [], "s": []})
        byp[r["prompt"]]["a" if r["tier"] == "answer_adopted" else "s"].append(
            overlap(r["prompt"], r["title"]))
    wins = ties = losses = 0
    for _p, g in byp.items():
        if not g["a"] or not g["s"]:
            continue
        ma, ms = statistics.mean(g["a"]), statistics.mean(g["s"])
        if ma > ms:
            wins += 1
        elif ma < ms:
            losses += 1
        else:
            ties += 1
    tot = wins + ties + losses
    print(f"  可配对 prompt 数 {tot}:采纳组重合更高 {wins} / 更低 {losses} / 持平 {ties}")
    if tot:
        print(f"  → 采纳组胜出率 {wins*100//tot}%")

    print("\n== 高频出现在【被采纳】标题里的 2-gram(去掉两组共有的)==")
    ca = Counter(g for r in ad for g in terms(r["title"]))
    cs = Counter(g for r in so for g in terms(r["title"]))
    na, ns = max(1, len(ad)), max(1, len(so))
    diff = {g: ca[g] / na - cs.get(g, 0) / ns for g in ca if ca[g] >= 3}
    for g, d in sorted(diff.items(), key=lambda x: -x[1])[:25]:
        print(f"    {g}  采纳组词频优势 {d:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
