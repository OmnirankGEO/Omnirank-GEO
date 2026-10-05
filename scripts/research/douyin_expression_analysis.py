"""被采纳抖音内容的**表达方式**分析 · B端/C端 + 行业维度

Owner 2026-08-03 追问:「B端的C端的，不同行业的，他们的表达方式是什么，
全部要研究出来，而不是草草结束」。

## 与既有两份报告的关系(先划边界,不重复研究)

已有(2026-08-01/02,对照实验 1,683 采纳 vs 3,923 仅检索):
  - 豆包**不是热度模型**(点赞中位 12 vs 14,分档一模一样)
  - **标题长度是最强单一信号**(全局中位 70 vs 55)
  - hashtag 对采纳零贡献;图文帖超配 +8pp
  - 卡面五骨架 / 首图钩子 / 文字层级 / 组内叙事

本次**新做**的是那两份没碰过的三块:
  1. B端 vs C端 的表达差异
  2. 行业间的表达差异(上一份只按行业看过形态占比,没看过表达)
  3. 「标题长度最强信号」这条结论**在行业层面还成不成立**

## 方法

判据 = **同一分层内**,带某个表达特征 vs 不带,采纳率差多少(lift)。
不是看"被采纳的内容里有多少 %带这个特征" —— 那个数会被特征本身的
流行度带偏(一个 90% 内容都有的特征,在采纳组里当然也占 90%)。

数据 = 生产飞轮 `geo_research_source_signals` 全量抖音信号(5,593 条),
`metadata->>'title'` 是标题面。**零 API 成本**,不抽样。

⚠️ 口径限制(别当成已知):
  - 只有**标题面**。卡面 OCR 文字、正文是否参与索引,仍未测(本机无视觉 API)。
  - 豆包信号 100% 是 **ai_search API 面**,APP 面零观测(既有结论,未变)。
  - 采纳率是"进了检索池之后"的条件概率,不是"发出去就被引"的概率。
"""
from __future__ import annotations

import csv
import math
import pathlib
import re
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
CSV = ROOT / ".tmp_research" / "douyin_titles.csv"

# ── B端 / C端 划分 ──
# 依据:这个行业的**买家**是企业还是个人消费者。
# 🔴 混合类单列,不硬塞进两端 —— 硬塞会让两端的结论都变脏。
B_SIDE = {"business_service", "technology", "geo_优化服务", "电梯行业"}
C_SIDE = {"时尚美妆", "food", "母婴亲子", "文娱游戏", "medical",
          "retail_ecommerce", "home_improvement", "auto", "tourism_hotel",
          "real_estate"}
MIXED = {"education", "finance"}

# ── 表达特征(正则即判据,可复现)──
FEATURES = {
    "疑问句式": r"怎么|哪家|哪个|如何|什么|值得吗|好不好|贵不贵|[?？]",
    "榜单式": r"排行|榜|TOP|top|前[一二三四五六七八九十\d]|[十百]大|推荐",
    "避坑式": r"避坑|踩坑|别买|别选|别乱|千万别|注意|坑|雷|翻车",
    "对比式": r"对比|区别|VS|vs|哪个好|还是|之争",
    "价格词": r"价格|多少钱|报价|费用|收费|万元|性价比|预算",
    "第一人称": r"我[的们]?|自己|亲测|实测|亲身",
    "干货教程": r"攻略|指南|教程|科普|知识|干货|方法|步骤|清单",
    "厂家自荐": r"厂家|源头|直销|自营|我们公司|本厂|工厂",
    "留联系方式": r"电话|微信|致电|咨询|联系|\d{3}[- ]?\d{4}[- ]?\d{4}",
    "含数字": r"\d",
    "情绪强化": r"绝了|太[好香绝]|真的|血泪|震惊|后悔|建议收藏|码住",
    "带年份": r"20\d{2}年?",
}
_COMPILED = {k: re.compile(v) for k, v in FEATURES.items()}


def load() -> list:
    rows = []
    with CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(
                ln for ln in f if not ln.startswith("SET")):
            title = (row.get("title") or "").strip()
            if not title:
                continue
            try:
                ln_ = int(row.get("len") or 0)
            except ValueError:
                continue
            rows.append({
                "industry": row.get("industry_key") or "",
                "adopted": row.get("signal_tier") == "answer_adopted",
                "len": ln_,
                "title": title,
            })
    return rows


def side_of(industry: str) -> str:
    if industry in B_SIDE:
        return "B端"
    if industry in C_SIDE:
        return "C端"
    if industry in MIXED:
        return "混合"
    return "其它"


def _wilson_lo(k: int, n: int) -> float:
    """Wilson 下界(95%)。**用它而不是裸比例**:小样本里 3/4=75% 这种数
    看着最漂亮,其实什么都说明不了。下界会把它压回去。"""
    if n == 0:
        return 0.0
    p = k / n
    z = 1.96
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - m) / d)


def rate(rows: list) -> tuple:
    n = len(rows)
    k = sum(1 for r in rows if r["adopted"])
    return k, n, (100.0 * k / n if n else 0.0)


def median(vals: list) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2


def feature_lift(rows: list, label: str) -> None:
    """同一分层内:带特征 vs 不带,采纳率差多少。"""
    print(f"\n── {label}(n={len(rows)})──")
    k, n, r = rate(rows)
    print(f"   基线采纳率 {r:.1f}%  ({k}/{n})")
    print(f"   {'特征':<10} {'带':>14} {'不带':>14} {'lift':>8}  判读")
    out = []
    for name, rx in _COMPILED.items():
        with_ = [x for x in rows if rx.search(x["title"])]
        without = [x for x in rows if not rx.search(x["title"])]
        if len(with_) < 30 or len(without) < 30:
            continue
        k1, n1, r1 = rate(with_)
        k2, n2, r2 = rate(without)
        lift = r1 - r2
        # 只有"带"的 Wilson 下界还高于"不带"的裸比例时,才敢说是正向
        solid = _wilson_lo(k1, n1) * 100 > r2
        out.append((abs(lift), name, r1, n1, r2, n2, lift, solid))
    for _, name, r1, n1, r2, n2, lift, solid in sorted(out, reverse=True):
        mark = "✅ 稳" if (solid and lift > 0) else ("↓" if lift < -2 else "·")
        print(f"   {name:<10} {r1:6.1f}% (n={n1:<5}) {r2:6.1f}% (n={n2:<5})"
              f" {lift:+7.1f}pp  {mark}")


def length_check(rows: list, label: str) -> None:
    """「标题长度是最强信号」在这一层还成不成立。"""
    ad = [r["len"] for r in rows if r["adopted"]]
    no = [r["len"] for r in rows if not r["adopted"]]
    if len(ad) < 20 or len(no) < 20:
        return
    print(f"   {label:<18} 采纳中位 {median(ad):>5.0f} 字 / 未采纳 {median(no):>5.0f} 字"
          f"  Δ{median(ad) - median(no):+6.0f}")


def main() -> int:
    if not CSV.exists():
        print(f"缺数据文件: {CSV}", file=sys.stderr)
        return 2
    rows = load()
    print(f"样本 {len(rows)} 条(生产飞轮全量抖音信号,零抽样)")

    # ① B端 / C端
    print("\n" + "=" * 72)
    print("① B端 vs C端 · 表达特征对采纳率的实际提升")
    print("=" * 72)
    by_side = defaultdict(list)
    for r in rows:
        by_side[side_of(r["industry"])].append(r)
    for side in ("B端", "C端", "混合"):
        if len(by_side[side]) >= 200:
            feature_lift(by_side[side], side)

    # ② 长度信号在各层还成不成立
    print("\n" + "=" * 72)
    print("② 「标题长度是最强信号」逐层复核")
    print("=" * 72)
    for side in ("B端", "C端", "混合"):
        length_check(by_side[side], side)
    print()
    by_ind = defaultdict(list)
    for r in rows:
        by_ind[r["industry"]].append(r)
    for ind, rs in sorted(by_ind.items(), key=lambda kv: -len(kv[1])):
        if len(rs) >= 100:
            length_check(rs, ind)

    # ③ 各行业最有效的表达
    print("\n" + "=" * 72)
    print("③ 分行业 · 最有效的表达特征(样本 >= 200 的行业)")
    print("=" * 72)
    for ind, rs in sorted(by_ind.items(), key=lambda kv: -len(kv[1])):
        if len(rs) >= 200:
            feature_lift(rs, f"{ind}({side_of(ind)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
