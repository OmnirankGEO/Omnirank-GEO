#!/usr/bin/env python
"""把外选终单 V2 的两份草单**机械抽**成 JSON(27 发)。

为什么不手抄
------------
手抄锚点是本轮最贵的错法:2026-08-26 我把 EXTB7-03 的行为臂 ``r2_62`` 抄成
``r2_61``(那是⑦的活性对照),于是"超出预期红集"被当成发现、按终单停机 ——
**停机的原因是我的转录错**。27 发 × 每发一到两个逐字锚,手抄必然再错一次。

所以:抽取器只做**无歧义**的抽取,凡是草单里写成人话的地方(「整行删除」、
``→ `... >= 0);` `` 这种带省略号的替换)一律标 ``NEEDS_AMENDMENT``,
由 runner 侧的 ``V2_AMENDMENTS`` 显式补,并写明理由。
**抽不出来 ≠ 猜一个**。

抽完还要过三道:
 ① 发数 = 27,且 ID 集合逐字等于 MUT-EXTE2-01..12 ∪ MUT-EXTE3-01..15;
 ② 每个锚在树上 ``count == 1``(草单作者实测过 1,我复核一次);
 ③ ``to != from`` 且 ``to`` 不像散文。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
DRAFTS = {
    "E2": Path(r"C:\AI-Test\EXTSEL_E2_DRAFT_2026-08-27.md"),
    "E3": Path(r"C:\AI-Test\EXTSEL_E3_DRAFT_2026-08-27.md"),
}
# 🔴 抽取产物进**仓**(.tiprun/),不进 .gate9/ 那堆 junit 噪音:
#    草单本身在仓外(C:\AI-Test\*.md),这份 JSON 是"到底跑了哪 27 发"
#    在仓里的**唯一**记录。上一轮的 extsel_ac.json 也是这么办的。
OUT = ROOT / ".tiprun" / "extsel_v2.json"

_SEC = re.compile(r"^### (MUT-EXTE[23]-\d{2})\b(.*)$")
_STOP = re.compile(r"^## 附注")
_FILE = re.compile(r"^- 文件:`([^`]+)`")
_LABEL = re.compile(r"^- (锚 ?[ab]?|替换)(?:\(|（|:|:)")
_ARROW = re.compile(r"^\s*(?:→|->)\s*`([^`]*)`\s*$")
_INLINE = re.compile(r"^\s{2,}`(.*)`\s*$")
_PRED = re.compile(r"^- 预测:(.*)$")
_EXACT_N = re.compile(r"恰\s*(\d+)\s*条")


def _prediction(lines: list[str]) -> dict:
    """从 ``- 预测:`` 那一行抽粗粒度预测。抽不出来标 ``?``,不猜。"""
    for ln in lines:
        m = _PRED.match(ln)
        if not m:
            continue
        body = m.group(1)
        # 「被杀」「杀,红集恰 N 条」「存活」「较可能存活」「最可能存活」
        killed = ("杀" in body.split("。")[0])
        survived = ("存活" in body.split("。")[0])
        verdict = "?"
        if killed and not survived:
            verdict = "killed"
        elif survived and not killed:
            verdict = "survived"
        elif killed and survived:
            # 「被杀…若 X 未跑则存活」这种带条件的,记 conditional 不硬判
            verdict = "conditional"
        n = _EXACT_N.search(body)
        return {"verdict": verdict, "exact_n": int(n.group(1)) if n else None,
                "text": body[:220]}
    return {"verdict": "?", "exact_n": None, "text": ""}


def _blocks(lines: list[str]) -> list[tuple[str, str, str]]:
    """把一节切成 (label, kind, payload) 序列。

    kind ∈ {``fence``, ``inline``, ``arrow``}。
    🔴 kind 必须显式记下来:是不是 fenced 决定要不要 dedent,而
       「是不是多行」只是**通常**与它同现的另一个信号 —— E2 单行锚是 inline、
       E3 单行锚是 fenced,拿多行当判据时 E3 九发全部多带 2 个空格、树上 count=0。
    """
    out: list[tuple[str, str, str]] = []
    i = 0
    pending: str | None = None
    while i < len(lines):
        ln = lines[i]
        m = _LABEL.match(ln)
        if m:
            pending = m.group(1).strip()
            # 标签行自己可能就带 inline 反引号内容(如 `- 锚(…):` 后同一行没有)
            i += 1
            continue
        if ln.strip().startswith("```"):
            fence, i2 = [], i + 1
            while i2 < len(lines) and not lines[i2].strip().startswith("```"):
                fence.append(lines[i2])
                i2 += 1
            payload = "\n".join(fence)
            out.append((pending or "?", "fence", payload))
            pending = None
            i = i2 + 1
            continue
        a = _ARROW.match(ln)
        if a:
            out.append(("→", "arrow", a.group(1)))
            i += 1
            continue
        inl = _INLINE.match(ln)
        if inl and pending:
            out.append((pending, "inline", inl.group(1)))
            pending = None
            i += 1
            continue
        i += 1
    return out


def _dedent_fence(payload: str) -> str:
    """草单里的 fenced 块整体缩进了 2 格(markdown 列表内),去掉这层缩进。

    🔴 只去**公共**前导两格,不做 strip:锚点的行首空白是**判别信息**
       (2026-08-26 实测:11 空格的锚被 16 空格的行子串命中过)。
    """
    lines = payload.split("\n")
    if all((not ln.strip()) or ln.startswith("  ") for ln in lines):
        lines = [ln[2:] if ln.startswith("  ") else ln for ln in lines]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def parse(fam: str, path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8").split("\n")
    secs: list[tuple[str, list[str]]] = []
    cur_id, buf, stopped = None, [], False
    for ln in raw:
        if _STOP.match(ln):
            stopped = True
        m = _SEC.match(ln)
        if m:
            if cur_id:
                secs.append((cur_id, buf))
            if stopped:
                cur_id, buf = None, []
                continue
            cur_id, buf = m.group(1), []
            continue
        if cur_id is not None:
            buf.append(ln)
    if cur_id:
        secs.append((cur_id, buf))

    out = []
    for mid, lines in secs:
        fpath = None
        for ln in lines:
            fm = _FILE.match(ln)
            if fm:
                fpath = fm.group(1)
                break
        blocks = _blocks(lines)
        pairs, why = [], []
        # 形态一:锚 → 替换(可能各自 fenced 或 inline)
        # 形态二:锚 a → (→ 或 替换),锚 b → (→ 或 替换)
        anchors = [b for b in blocks if b[0].startswith("锚")]
        repls = [b for b in blocks if b[0] in ("替换", "→")]
        if len(anchors) == len(repls) and anchors:
            for (_alb, akind, apl), (_rlb, rkind, rpl) in zip(anchors, repls):
                a = _dedent_fence(apl) if akind == "fence" else apl
                b = _dedent_fence(rpl) if rkind == "fence" else rpl
                pairs.append({"from": a, "to": b})
        else:
            why.append(f"锚 {len(anchors)} 个 / 替换 {len(repls)} 个,配不上")
        for pr in pairs:
            if "..." in pr["to"] or "…" in pr["to"]:
                why.append("替换里有省略号,不是逐字替换")
            if not pr["from"].strip():
                why.append("锚为空")
            if pr["from"] == pr["to"]:
                why.append("替换与锚相同")
        out.append({"id": mid, "family": fam, "file": fpath, "pairs": pairs,
                    "predict": _prediction(lines),
                    "needs_amendment": sorted(set(why))})
    return out


def main() -> int:
    all_m: list[dict] = []
    for fam, path in DRAFTS.items():
        if not path.exists():
            raise SystemExit(f"🔴 草单不在:{path}")
        got = parse(fam, path)
        print(f"{fam}:抽到 {len(got)} 发")
        all_m.extend(got)

    # 🔴 同 ID 多节:只允许「一个正文 + N 个空壳交叉引用」。
    #    E3 草单的 MUT-EXTE3-10 就是这样(E3-3 组写了「列于 E3-1 组…不重计」)。
    #    **不许默默去重** —— 默默去重正是丢发数的方式;折叠必须打印出来。
    by_id: dict[str, list[dict]] = {}
    for m in all_m:
        by_id.setdefault(m["id"], []).append(m)
    collapsed = []
    for mid, group in by_id.items():
        if len(group) == 1:
            continue
        real = [g for g in group if g["pairs"]]
        shell = [g for g in group if not g["pairs"]]
        if len(real) != 1:
            raise SystemExit(
                f"🔴 {mid} 有 {len(group)} 节、其中 {len(real)} 节带锚 —— "
                "同 ID 只允许一个正文节。停下人工核对,不自行去重。")
        for g in shell:
            all_m.remove(g)
        collapsed.append(f"{mid}(折叠 {len(shell)} 个空壳交叉引用节)")
    if collapsed:
        print("交叉引用节折叠:" + " · ".join(collapsed))

    want = ([f"MUT-EXTE2-{i:02d}" for i in range(1, 13)]
            + [f"MUT-EXTE3-{i:02d}" for i in range(1, 16)])
    got_ids = [m["id"] for m in all_m]
    dupes = sorted({i for i in got_ids if got_ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"🔴 重复 ID {dupes}")
    if sorted(got_ids) != sorted(want):
        missing = sorted(set(want) - set(got_ids))
        extra = sorted(set(got_ids) - set(want))
        raise SystemExit(
            f"🔴 ID 集合对不上 —— 缺 {missing} · 多 {extra} · "
            f"实得 {len(got_ids)} 个 / 应为 {len(want)} 个")
    if len(all_m) != 27:
        raise SystemExit(f"🔴 抽到 {len(all_m)} 发,终单是 27")

    print(f"\n机械枚举:E2={sum(1 for m in all_m if m['family'] == 'E2')} "
          f"E3={sum(1 for m in all_m if m['family'] == 'E3')} 合计={len(all_m)}")

    # 锚点在树上复核 count == 1
    print("\n── 锚点树上复核(count 必须 = 1)──")
    bad = []
    for m in all_m:
        if m["needs_amendment"]:
            print(f"   ⏸  {m['id']:<16} 待人工补:{m['needs_amendment']}")
            continue
        f = ROOT / (m["file"] or "")
        if not f.exists():
            print(f"   🔴 {m['id']:<16} 文件不在:{m['file']}")
            bad.append(m["id"])
            continue
        raw = f.read_bytes()
        for k, pr in enumerate(m["pairs"]):
            needle = pr["from"].replace("\r\n", "\n").encode("utf-8")
            n = raw.count(needle)
            n_crlf = raw.count(needle.replace(b"\n", b"\r\n"))
            hit = n if n else n_crlf
            pr["anchor_count"] = hit
            flag = "✅" if hit == 1 else "🔴"
            if hit != 1:
                bad.append(f"{m['id']}#{k}")
                # 停机的人要**现场**,不是结论:把最常见的两种偏差各试一次。
                probe = {}
                for lbl, cand in (
                        ("少2空格", b"\n".join(x[2:] if x.startswith(b"  ") else x
                                              for x in needle.split(b"\n"))),
                        ("多2空格", b"\n".join(b"  " + x for x in needle.split(b"\n"))),
                        ("strip首尾", needle.strip())):
                    probe[lbl] = raw.count(cand)
                print(f"      诊断:{probe}(锚 = {needle[:70]!r})")
            print(f"   {flag} {m['id']:<16} 锚{k} count={hit}  "
                  f"{pr['from'].splitlines()[0][:58]!r}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(all_m, ensure_ascii=False, indent=1), encoding="utf-8")
    kv = {}
    for m in all_m:
        kv[m["predict"]["verdict"]] = kv.get(m["predict"]["verdict"], 0) + 1
    print(f"\n预测分布(机械抽自草单「- 预测:」行):{kv}")
    unk = [m["id"] for m in all_m if m["predict"]["verdict"] == "?"]
    if unk:
        print(f"⚠️  预测抽不出来的:{unk}(按 ? 记,不猜)")
    print(f"\n写出 {OUT}")
    pend = [m["id"] for m in all_m if m["needs_amendment"]]
    if pend:
        print(f"⏸  {len(pend)} 发需要显式 amendment(抽不出来**不猜**):{pend}")
    if bad:
        print(f"🔴 锚点复核失败:{bad}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
