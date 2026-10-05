#!/usr/bin/env python3
"""WO_295 · 登记表期望文件构建器:合车前算出三张登记表「合成后应当长什么样」(三态)。

## 为什么要这一把
合车时 `tests/MUST_RUN.txt` / `tests/ONESHOT_RUNS.txt` / `tests/RETIRED_TESTS.txt` 几乎每班都冲突
(一边改形、一边追加),以前由 Review 手写期望文件再逐条对。09-24 AC:现役 dfee722a3 上 A 改了
`test_geo_douyin_card_templates.py` 那行「守什么」的措辞,手写构建器按「基线行赢」把**已上线的措辞
回退**了,Deploy 逐提交查才纠正。病根:只拿改形支和交付比,**没把现役尖当成一方**。

## 规则(逐张表、按主键 = 第一栏(路径或 路径::格)逐条三方解)
每个输入分支 X(改形支 + 各交付)各自取 mb = merge-base(X, 现役尖),看 X 相对 mb 改了哪些键:
  · X 没改这条(X 的值 == mb 的值)⇒ X 不表态;
  · X 改了、现役相对 mb 没改 ⇒ 取 X 的(**只有一边改过就取那一边**);
  · X 改了、现役也改了、两者相同 ⇒ 无冲突;
  · 两边都改且**形状(栏数)不同** ⇒ 与改形支同形的那一边赢(改形版赢);
  · 两边都改且**同形** ⇒ 红,要人定。
  多个分支对同一键给出不同值,两两按同一规则裁。
表头(第一条目之前的全部注释 / 空行,含预算行):只有一边改过 ⇒ 取那一边;两边都改 ⇒ **逐行三方合并**
  (git merge-file),合得干净就取合并结果(⚠️ 点名);有冲突 ⇒ 取改形支,但**红**,并列出会丢掉的那几行
  —— 人看过确认可丢,加 `--accept-header-loss <表名>` 降为 ⚠️。
  (Review 原规则是「表头取改形支」;09-24 实测它在 AC 上把 A(WO_258)写进现役表头的
   「detail_ui 已修绿并登记」回退成「detail_ui 不在这里」—— 与病例同一类病,所以两边都改时不再整块取一边。)
出口核:输出对**现役尖**逐条 diff,每一条「与现役不同」(改 / 增 / 删 / 表头)都必须能在它声称的来源 sha 的
那张表里**逐字**找回来,指不出 ⇒ 红;输出里栏数与改形支不同形的行 ⇒ 红(没人给出改形版,要人补)。

## 退出码
  0 = 三张表都解完、每条差异都指得出来源
  1 = 有同形冲突 / 指不出来源 / 不同形的残行
  3 = 没跑成(sha 解析不了、git 取不到对象)
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

FILES = {"MUST_RUN": "tests/MUST_RUN.txt", "ONESHOT_RUNS": "tests/ONESHOT_RUNS.txt",
         "RETIRED_TESTS": "tests/RETIRED_TESTS.txt"}
REPO = ""


class CantRun(Exception):
    pass


def git(*args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True)
    return r.returncode, r.stdout.decode("utf-8", errors="replace")


def resolve(sha: str) -> str:
    rc, out = git("rev-parse", "--verify", "-q", f"{sha}^{{commit}}")
    if rc:
        raise CantRun(f"{sha} 解析不了")
    return out.strip()


_mb_cache: dict[tuple[str, str], str] = {}


def merge_base(a: str, b: str) -> str:
    if (a, b) not in _mb_cache:
        rc, out = git("merge-base", a, b)
        if rc:
            raise CantRun(f"{a[:9]} 与 {b[:9]} 没有共同祖先")
        _mb_cache[(a, b)] = out.strip()
    return _mb_cache[(a, b)]


def is_entry(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


def parse(sha: str, rel: str) -> tuple[str | None, dict[str, str], list[str]]:
    """(表头文本 | None=文件不存在, {主键: 整行}, 主键顺序)。"""
    rc, out = git("show", f"{sha}:{rel}")
    if rc:
        rc2, _ = git("cat-file", "-e", f"{sha}^{{commit}}")
        if rc2:
            raise CantRun(f"{sha[:9]} 取不到")
        return None, {}, []
    lines = out.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    head: list[str] = []
    rows: dict[str, str] = {}
    order: list[str] = []
    seen_entry = False
    for ln in lines:
        if is_entry(ln):
            seen_entry = True
            k = ln.split("\t", 1)[0].rstrip()
            if k in rows:
                raise CantRun(f"{sha[:9]}:{rel} 主键重复:{k}")
            rows[k] = ln
            order.append(k)
        elif not seen_entry:
            head.append(ln)
        # 条目之后的注释 / 空行:三张表实测都没有;有也不参与合成(不影响键)
    return "\n".join(head), rows, order


def shape(line: str | None) -> int | None:
    return None if line is None else len(line.split("\t"))


def merge3(ours: str, base: str, theirs: str) -> tuple[str, int]:
    """逐行三方合并(git merge-file -p):返回 (合并结果, 冲突数)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for nm, t in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = Path(d) / nm
            p.write_bytes((t + "\n").encode("utf-8"))
            paths.append(str(p))
        r = subprocess.run(["git", "merge-file", "-p", *paths], capture_output=True)
        if r.returncode < 0 or r.returncode > 127:
            raise CantRun("git merge-file 跑不起来")
        text = r.stdout.decode("utf-8", errors="replace")
        return (text[:-1] if text.endswith("\n") else text), r.returncode


def audit(name: str, rel: str, l_head, l_rows: dict, l_order: list, head_val, head_src: str,
          head_parts: dict, cur: dict, src: dict, order: list) -> tuple[list[str], list[str]]:
    """出口核:输出对现役逐条 diff,每条差异必须在它声称的来源 sha 的那张表里逐字找回。返回 (红, 来源说明)。"""
    red: list[str] = []
    prov: list[str] = []
    for k in order:
        if l_rows.get(k) == cur[k]:
            continue
        s = src.get(k, "live")
        _, s_rows, _ = parse(s, rel) if s != "live" else (None, l_rows, None)
        if s == "live" or s_rows.get(k) != cur[k]:
            red.append(f"{name} `{k}`:与现役不同,但在声称的来源 {s[:9]} 里找不到这一行 ⇒ 指不出来源")
        else:
            prov.append(f"{name} {'改' if k in l_rows else '增'} `{k}` ← {s[:9]}")
    for k in l_order:
        if k not in cur:
            s = src.get(k, "live")
            _, s_rows, _ = parse(s, rel) if s != "live" else (None, l_rows, None)
            if s == "live" or k in s_rows:
                red.append(f"{name} `{k}`:现役有、输出没有,但 {s[:9]} 里这一行还在 ⇒ 指不出来源")
            else:
                prov.append(f"{name} 删 `{k}` ← {s[:9]}")
    if head_val != l_head:
        if head_src.startswith("merge("):
            h_of_src = head_parts.get(head_src)
        else:
            h_of_src = parse(head_src, rel)[0] if head_src != "live" else l_head
        if head_src == "live" or h_of_src != head_val:
            red.append(f"{name} 表头与现役不同,但指不出来源")
        else:
            prov.append(f"{name} 表头 ← {head_src[:9]}")
    return red, prov


def build(name: str, rel: str, live: str, reshape: str, deliveries: list[str],
          accept_loss: frozenset[str] = frozenset()):
    """返回 (输出文本 | None, 红 list, 警告 list, 来源说明 list)。"""
    red: list[str] = []
    warn: list[str] = []
    prov: list[str] = []
    l_head, l_rows, l_order = parse(live, rel)
    r_head, r_rows, _ = parse(reshape, rel)
    target = {shape(v) for v in r_rows.values()}
    target_shape = target.pop() if len(target) == 1 else None

    # 每个键:当前值 + 来源;先放现役
    cur: dict[str, str | None] = dict(l_rows)
    src: dict[str, str] = {k: "live" for k in l_rows}
    appended: list[str] = []
    head_val, head_src = l_head, "live"
    head_parts: dict[str, str] = {}      # 三方合并出来的表头,出口核用

    def winner(a: tuple[str | None, str], b: tuple[str | None, str], key: str):
        """两边都改过:a=(值, 来源) 当前,b=(值, 来源) 新来的。"""
        (va, sa), (vb, sb) = a, b
        if va == vb:
            return a
        sh_a, sh_b = shape(va), shape(vb)
        if sh_a != sh_b and target_shape is not None and target_shape in (sh_a, sh_b):
            win = a if sh_a == target_shape else b
            warn.append(f"{name} `{key}`:{sa[:9]} 与 {sb[:9]} 都改了且不同形 ⇒ 取与改形支同形({target_shape} 栏)的 {win[1][:9]}")
            return win
        red.append(f"{name} `{key}`:{sa[:9]} 与 {sb[:9]} 都改了且{'同形' if sh_a == sh_b else '都不与改形支同形'} ⇒ 要人定"
                   f"\n        {sa[:9]}: {va!r}\n        {sb[:9]}: {vb!r}")
        return a

    for x in [reshape, *deliveries]:
        mb = merge_base(x, live)
        m_head, m_rows, _ = parse(mb, rel)
        x_head, x_rows, x_order = parse(x, rel)
        if x_head is None and m_head is None:
            continue
        # 表头:X 相对 mb 改过才表态;两边都改 ⇒ 逐行三方合并(git merge-file),不整块取一边
        #   🔴 09-24 实证:整块取改形支会把现役 A(WO_258)改好的表头注释回退成
        #      「detail_ui **不在这里**」,而 detail_ui 那行明明在表里 —— 与病例同一类病,只是发生在表头。
        if x_head is not None and x_head != m_head:
            if head_src == "live" and l_head == m_head:
                head_val, head_src = x_head, x            # 只有 X 改了
            elif x_head == head_val:
                pass                                      # 改成同样的,无冲突
            else:
                merged, n_conf = merge3(head_val or "", m_head or "", x_head)
                if n_conf == 0:
                    head_val, head_src = merged, f"merge({head_src[:9]}+{x[:9]})"
                    head_parts[head_src] = merged
                    warn.append(f"{name} 表头:当前(来自 {head_src.split('(')[-1]})与 {x[:9]} 都改了 ⇒ 逐行三方合并干净")
                else:
                    keep_x = (x == reshape)
                    kept, lost_src = (x_head, head_src) if keep_x else (head_val, x)
                    lost_text = head_val if keep_x else x_head
                    lost = [ln for ln in (lost_text or "").split("\n") if ln and ln not in kept.split("\n")]
                    msg = (f"{name} 表头:{x[:9]} 与当前表头(来自 {head_src[:9]})逐行合并有 {n_conf} 处冲突 ⇒ 取改形支,"
                           f"会丢掉 {lost_src[:9]} 的 {len(lost)} 行:" + "".join(f"\n          - {ln}" for ln in lost[:8]))
                    (warn if name in accept_loss else red).append(
                        msg + ("\n        (已用 --accept-header-loss 确认可丢)" if name in accept_loss
                               else "\n        人看过确认可丢 ⇒ 加 --accept-header-loss " + name))
                    if keep_x:
                        head_val, head_src = x_head, x
        # 条目:按 X 文件里的顺序走(决定追加顺序),再补 mb 里有、X 删掉的
        for k in x_order + [k for k in m_rows if k not in x_rows]:
            bx, vx = m_rows.get(k), x_rows.get(k)
            if bx == vx:
                continue                      # X 没改这条
            lv = l_rows.get(k)
            if lv == bx and src.get(k, "live") == "live":
                new = (vx, x)                 # 只有 X 改了(现役相对 mb 没动)
            else:
                new = winner((cur.get(k), src.get(k, "live")), (vx, x), k)
            if new[0] is None:
                cur.pop(k, None)
                src[k] = new[1]
            else:
                if k not in cur and k not in appended and k not in l_rows:
                    appended.append(k)
                cur[k] = new[0]
                src[k] = new[1]

    if head_val is None and not cur:
        return None, red, warn, prov
    order = [k for k in l_order if k in cur] + [k for k in appended if k in cur]
    out_lines = ([head_val] if head_val else []) + [cur[k] for k in order]
    text = "\n".join(out_lines) + "\n"

    a_red, a_prov = audit(name, rel, l_head, l_rows, l_order, head_val, head_src, head_parts,
                          cur, src, order)
    red += a_red
    prov += a_prov
    if target_shape is not None:
        for k in order:
            if shape(cur[k]) != target_shape:
                red.append(f"{name} `{k}` 是 {shape(cur[k])} 栏,改形支是 {target_shape} 栏,且没有任何输入给出改形版 ⇒ 要人补")
    return text, red, warn, prov


def main(argv: list[str] | None = None) -> int:
    global REPO
    _mb_cache.clear()      # 进程内连续调用时不许串到下一次
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--live", required=True, help="现役尖 sha")
    ap.add_argument("--reshape", required=True, help="改形支(基线)sha")
    ap.add_argument("--delivery", action="append", default=[], help="各交付 sha(可多次,顺序 = 追加顺序)")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--tag", default="", help="输出文件名里的班次号,如 0913AC")
    ap.add_argument("--accept-header-loss", action="append", default=[], choices=sorted(FILES),
                    help="人看过、确认该表表头合并冲突时丢掉的行可以丢(红降为 ⚠️,并写进输出)")
    a = ap.parse_args(argv)
    REPO = a.repo
    try:
        live = resolve(a.live)
        reshape = resolve(a.reshape)
        deliveries = [resolve(d) for d in a.delivery]
        print(f"  现役 {live[:9]} · 改形支 {reshape[:9]} · 交付 {' '.join(d[:9] for d in deliveries) or '(无)'}")
        acc = frozenset(a.accept_header_loss)
        results = {n: build(n, rel, live, reshape, deliveries, acc) for n, rel in FILES.items()}
    except CantRun as e:
        print(f"  🔴 本工具【没跑成·rc=3】:{e}")
        return 3
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    any_red = False
    for n, (text, red, warn, prov) in results.items():
        if text is None:
            print(f"  ⚪ {n}:所有输入里都没有这张表(无对象)")
            continue
        f = out / f"rv_expected_{a.tag + '_' if a.tag else ''}{n}.txt"
        f.write_bytes(text.encode("utf-8"))
        n_rows = sum(1 for ln in text.split("\n") if is_entry(ln))
        print(f"  {n}:{n_rows} 条 · sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]} → {f}")
        for p in prov:
            print(f"      · {p}")
        for w in warn:
            print(f"      ⚠️  {w}")
        for r in red:
            print(f"      🔴 {r}")
        any_red = any_red or bool(red)
    if any_red:
        print("  🔴 有要人定 / 指不出来源的条目 —— 期望文件已写出,但**不能直接用**")
        return 1
    print("  ✅ 三张表都解完,每条与现役的差异都指得出来源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
