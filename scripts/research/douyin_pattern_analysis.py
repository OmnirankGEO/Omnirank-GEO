"""§6 深挖 · 被采纳抖音条目的【标题/文案/卡片】形态统计

产出 DOUYIN_ADOPTED_CONTENT_PATTERNS_2026-08.md 的数字来源。
只做统计,不调外部 API(输入 = douyin_adopted_content_probe.py 的产物)。

统计维度按工单 §2 要校准的四件事组织:
  1. 卡片张数分布      -> 卡片模板要出几张
  2. 标题形态          -> 标题引擎模板池
  3. hashtag 数量/形态 -> 变体 hashtag 策略
  4. 文案长度/结构     -> 图文文案 prompt 的字数与分段约束

用法:  python scripts/research/douyin_pattern_analysis.py --probe .tmp_ro/probe_result.json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List

# §1.3/§1.4 实证形态的判据词表(用于统计"被采纳内容里这些形态占多少")
_RANK_PAT = re.compile(
    r"(排行|排行榜|榜单|top\s*\d+|前\s*\d+\s*名|十强|五强|\d+\s*大(?:品牌|机构|公司|厂家))",
    re.I,
)
_CHOICE_PAT = re.compile(r"(哪家好|哪家强|怎么选|如何选|选购|推荐|口碑|靠谱|值得|测评|对比)")
_PITFALL_PAT = re.compile(r"(避坑|坑|千万别|不要买|智商税|翻车|注意事项|骗)")
_QUESTION_PAT = re.compile(r"[?？]|(哪家|怎么|如何|为什么|值不值|要不要|多少钱)")
_NUMBER_PAT = re.compile(r"\d")
_YEAR_PAT = re.compile(r"(20\d{2}\s*年?)")
# 城市:常见地级市后缀 + 直辖市/一线
_CITY_PAT = re.compile(
    r"(北京|上海|广州|深圳|杭州|成都|重庆|武汉|南京|西安|苏州|天津|长沙|郑州|青岛|"
    r"东莞|宁波|佛山|合肥|无锡|昆明|济南|福州|厦门|哈尔滨|沈阳|大连|温州|石家庄|南宁|"
    r"[一-龥]{2,3}市)"
)


def pct(n: int, d: int) -> str:
    return f"{n*100//d}%" if d else "-"


def _bucket(vals: List[float], edges: List[float]) -> List[str]:
    out = []
    for e in edges:
        out.append(f"<={int(e)}: {len([v for v in vals if v <= e])}/{len(vals)}")
    return out


def analyze(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    img = [r for r in rows if r.get("kind") == "image_post"]
    vid = [r for r in rows if r.get("kind") == "video"]
    both = img + vid

    def texts(rs):
        # 标题面 = signal 里的 title(豆包看到的那一面)+ 抖音 desc
        return [(r.get("title_from_signal") or r.get("desc") or "") for r in rs]

    out: Dict[str, Any] = {}
    out["counts"] = {
        "total": len(rows), "image_post": len(img), "video": len(vid),
        "other": len(rows) - len(both),
    }

    # 1. 卡片张数
    ic = [r["image_count"] for r in img if r.get("image_count")]
    out["image_cards"] = {
        "n": len(ic),
        "median": statistics.median(ic) if ic else 0,
        "dist": dict(sorted(collections.Counter(ic).items())),
        "in_3_9": len([c for c in ic if 3 <= c <= 9]),
        "eq_1": len([c for c in ic if c == 1]),
    }

    # 2. 视频时长
    ds = [float(r.get("duration_sec") or 0) for r in vid if r.get("duration_sec")]
    out["video_duration"] = {
        "n": len(ds),
        "median": round(statistics.median(ds), 1) if ds else 0,
        "mean": round(statistics.mean(ds), 1) if ds else 0,
        "buckets": _bucket(ds, [30, 60, 90, 120, 180, 300, 600]) if ds else [],
    }

    # 3. 标题形态(分形态各统计一次,别只报合计)
    for label, rs in (("image_post", img), ("video", vid), ("all", both)):
        ts = [t for t in texts(rs) if t]
        n = len(ts)
        out[f"title_{label}"] = {
            "n": n,
            "rank": pct(len([t for t in ts if _RANK_PAT.search(t)]), n),
            "choice": pct(len([t for t in ts if _CHOICE_PAT.search(t)]), n),
            "pitfall": pct(len([t for t in ts if _PITFALL_PAT.search(t)]), n),
            "question": pct(len([t for t in ts if _QUESTION_PAT.search(t)]), n),
            "has_number": pct(len([t for t in ts if _NUMBER_PAT.search(t)]), n),
            "has_year": pct(len([t for t in ts if _YEAR_PAT.search(t)]), n),
            "has_city": pct(len([t for t in ts if _CITY_PAT.search(t)]), n),
            "len_median": statistics.median([len(t) for t in ts]) if ts else 0,
        }

    # 4. hashtag
    tags_per = [len(r.get("text_extra") or []) for r in both]
    with_tag = [t for t in tags_per if t > 0]
    all_tags = [t for r in both for t in (r.get("text_extra") or [])]
    out["hashtags"] = {
        "posts_with_tag": f"{len(with_tag)}/{len(tags_per)} ({pct(len(with_tag), len(tags_per))})",
        "median_when_present": statistics.median(with_tag) if with_tag else 0,
        "max": max(tags_per) if tags_per else 0,
        "top20": collections.Counter(all_tags).most_common(20),
    }

    # 5. 文案(desc)长度
    descs = [r.get("desc") or "" for r in both]
    dl = [len(d) for d in descs if d]
    out["desc_len"] = {
        "n": len(dl),
        "median": statistics.median(dl) if dl else 0,
        "buckets": _bucket([float(x) for x in dl], [50, 100, 200, 300, 500, 800]) if dl else [],
    }

    # 6. 家装(试点行业)单独看
    hi = [r for r in rows if r.get("industry_key") == "home_improvement"]
    hi_img = len([r for r in hi if r.get("kind") == "image_post"])
    hi_vid = len([r for r in hi if r.get("kind") == "video"])
    hi_t = [t for t in texts(hi) if t]
    out["home_improvement"] = {
        "total": len(hi), "image_post": hi_img, "video": hi_vid,
        "image_share": pct(hi_img, hi_img + hi_vid),
        "rank": pct(len([t for t in hi_t if _RANK_PAT.search(t)]), len(hi_t)),
        "choice": pct(len([t for t in hi_t if _CHOICE_PAT.search(t)]), len(hi_t)),
        "has_city": pct(len([t for t in hi_t if _CITY_PAT.search(t)]), len(hi_t)),
        "titles": hi_t[:15],
    }

    # 7. 行业 x 形态
    m: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        if r.get("kind") == "image_post":
            m[r["industry_key"]][0] += 1
        elif r.get("kind") == "video":
            m[r["industry_key"]][1] += 1
    out["by_industry"] = {
        k: {"image_post": v[0], "video": v[1], "image_share": pct(v[0], v[0] + v[1])}
        for k, v in sorted(m.items(), key=lambda x: -(x[1][0] + x[1][1]))
    }
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--probe", required=True)
    p.add_argument("--out", default="")
    args = p.parse_args()

    rows = json.loads(Path(args.probe).read_text(encoding="utf-8"))
    res = analyze(rows)
    txt = json.dumps(res, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(txt, encoding="utf-8")
        print(f"[analysis] 写入 {args.out}")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
