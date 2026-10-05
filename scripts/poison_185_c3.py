# -*- coding: utf-8 -*-
"""#185 c3 注毒闸 —— 专打**这一单新加的那几把锁**。

c3 把「防御型公司词」的题面从模板直出改成走 LLM,于是新增了四条路:
  ① 防御槽进 LLM 批(提示块带品牌名 / 问名 / playbook 角度)
  ② 回来逐槽把关(缺品牌名 / 超长 / 重复 ⇒ 落模板并标 source)
  ③ playbook 按品牌蒸一次、缓存 7 天、知识库变或换版即失效
  ④ 正文侧**单点**读同一份 playbook
每一条都要有一发毒证明它有牙 —— 否则「判据全绿」只说明判据没在看。

🔴 毒仍绿有四解:锁没牙 / 毒没下成 / 毒够不着 / 目标本来就冗余。
   本脚本对前两解自带检查(锚必须恰好命中 1 次、sha 必须变),
   后两解要人读 `抓住它的:` 那一栏自己判。
"""
import hashlib
import io
import os
import re
import subprocess
import sys

#: pytest 短摘要行:「55 passed, 17 warnings in 180.73s」/「1 failed, 54 passed …」
_SUMMARY_RE = re.compile(r"\b\d+ (passed|failed|error|errors|skipped)\b")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/defensive_direction_2026_09_13"
#: c2 与 c3 判据**一起跑**:c3 的改动直接改了 c2 那条路,
#: 只跑 c3 的话,c2 的红会被当成"没这回事"。
EXPECTED_N = 67

SRV = "server.py"
DQ = "writing/defensive_questions.py"
PB = "writing/defensive_playbook.py"
KTG = "writing/keyword_topic_generator.py"
AW = "writing/article_writer.py"

_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_185_test"
ENV = dict(os.environ)
ENV["TEST_DATABASE_URL"] = _DSN
ENV["DATABASE_URL"] = _DSN
ENV["PYTHONIOENCODING"] = "utf-8"


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def read(path):
    return io.open(os.path.join(ROOT, path), encoding="utf-8").read()


def write(path, text):
    io.open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="\n").write(text)


def run():
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    # 🔴 仪器坏了要**出声**:没有摘要行就不是"0 失败",是没跑成。
    # 🔴 别拿 `=` 当摘要行的特征:`-q --no-header` 下 pytest 打的是
    #    「55 passed, 17 warnings in 180.73s」,一个等号都没有。
    #    第一版就是这么写的,于是一次**完全正常**的全绿基线被判成"仪器坏了" ——
    #    自检自己误报,和自检不报一样糟:它会让人去修没坏的东西。
    summary = [l for l in out.splitlines()
               if _SUMMARY_RE.search(l) and " in " in l]
    if not summary and "no tests ran" not in out:
        raise RuntimeError("pytest 没给出摘要行 —— 仪器坏了,不是全绿:\n" + out[-2000:])
    failed = set()
    for line in out.splitlines():
        if not (line.startswith("FAILED ") or line.startswith("ERROR ")):
            continue
        parts = line.split()
        if len(parts) < 2 or "::" not in parts[1]:
            continue
        failed.add(parts[1].split(" - ")[0])
    c = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    total = len([l for l in c.stdout.decode("utf-8", "replace").splitlines()
                 if "::" in l])
    return failed, total


POISONS = [
    # 🔴 **阴性对照**(唯一一发应当保持绿的)。全红的毒表证明不了判据有区分力 ——
    #    它同样可能是"随便改点什么都红"。
    ("NC-阴性对照:playbook 模块加一句无害注释(应当**绿**)", PB,
     "def _rollback_quietly(conn) -> None:",
     "# 阴性对照注入的一行,无行为影响\ndef _rollback_quietly(conn) -> None:",
     "字节变了、行为没变 —— 绿才对。红了说明有判据在钉形状不钉行为。"),

    # ── ① 防御槽进 LLM ────────────────────────────────────────
    ("P1-防御篇又被摘出批(退回 c2 的模板直出)", SRV,
     '        _touched = {r["topic_id"] for r in defensive_unconverted}',
     '        _touched = ({r["topic_id"] for r in defensive_unconverted}\n'
     '                    | {r["topic_id"] for r in defensive_applied})',
     "Owner 要的就是「防御题也过 AI」。退回去之后端点仍然返标题、仍然 200,"
     "只是那些标题又变回模板 —— 回包形状一模一样。"),

    ("P2-生成器不给防御槽豁免关键词身份复核", KTG,
     "                if _is_defensive and not identity_conflict:",
     "                if False and not identity_conflict:",
     "防御题的商业对象是品牌不是本篇关键词 ⇒ 复核逐条判不过 ⇒ 每槽落模板。"
     "生产上表现成「接了 AI 但产出跟以前一样」,全程零报错。"),

    ("P3-防御槽重新参加问句配比改写", KTG,
     "        topics, _defensive_topics = self._split_defensive(topics)\n"
     "        for _t in _defensive_topics:",
     "        _defensive_topics = []\n"
     "        for _t in _defensive_topics:",
     "配比会把本篇 original_keyword 当锚点续到标题后面,得到病句;"
     "反向的回落还会把八问的问句改成陈述句。"),

    # ── c3'' 标题上限:数值本身要有人钉,且全仓只有一份 ─────────
    ("X2-Review 那一发:_MAX_TITLE_CHARS 48->480", DQ,
     "_MAX_TITLE_CHARS: int = 48", "_MAX_TITLE_CHARS: int = 480",
     "c3' 时这一发**全包绿**:too_long 那条判据用 `_MAX_TITLE_CHARS + 1` 造标题,"
     "钉的是机制,阈值一起动就一起绿 —— 对数值漂移完全是瞎的。"),

    ("Y1-生成器提示词改回裸字面量 48(第二份拷贝)", KTG,
     '""" % (chr(10).join(_d_lines), self.brand_name, _MAX_TITLE_CHARS)',
     '""" % (chr(10).join(_d_lines), self.brand_name, 48)',
     "提示词告诉 LLM 的上限与 vet 把关的上限一旦成为两份拷贝,"
     "改一处另一处静默不同意:提示说 480、把关按 48 ⇒ LLM 照 480 写、"
     "回来全判 too_long 落模板,全程零报错。"),

    ("Y2-Review 那一发:模板写死 48、元组去常量,注释与 import 都留着", KTG,
     "4. 一句话说清,不堆形容词;标题不超过 %d 字" + chr(10) +
     chr(34) + chr(34) + chr(34) + " % (chr(10).join(_d_lines), self.brand_name, _MAX_TITLE_CHARS)",
     "4. 一句话说清,不堆形容词;标题不超过 48 字" + chr(10) +
     chr(34) + chr(34) + chr(34) + " % (chr(10).join(_d_lines), self.brand_name)",
     "源码正则那条锁认的是我当时写的那一种形状 —— 换个写法把同一件事做回去,"
     "它照样绿。c3''' 改成行为锁:真渲染一次,断言发出去的那句话跟着常量走。"),

    # ── ② 把关与兜底 ──────────────────────────────────────────
    ("P4-把关放行「标题里没有品牌名」", DQ,
     "        elif not title_carries_brand(candidate, brand):\n"
     '            reason = "missing_brand"',
     "        elif False:\n"
     '            reason = "missing_brand"',
     "没有主语的公司词题 = 给同行做嫁衣。放行之后标题照出、source 还标 llm。"),

    ("P5-把关永不兜底(全部标成 llm)", DQ,
     "        if reason:\n"
     '            title, source = template_title, "template_fallback"',
     "        if False:\n"
     '            title, source = template_title, "template_fallback"',
     "静默兜底最坏,而「永不兜底」是它的反面:漏槽那一篇会留着旧标题。"),

    ("P6-跨批去重被取消", DQ,
     "        elif candidate in seen:\n"
     '            reason = "duplicate_title"',
     "        elif False:\n"
     '            reason = "duplicate_title"',
     "两条不同问名写成同一句话,对用户就是重复。"),

    # ── ③ 计费(当前口径:防御篇不进基数) ──────────────────────
    ("P7-扣费改按 updated(防御篇又进基数)", SRV,
     "                actual_extra = (_billable_updated - 1) * _topic_base",
     "                actual_extra = (updated - 1) * _topic_base",
     "c3 把防御篇放进批里之后,`updated` 含它们 ⇒ 按篇收了不该收的钱。"
     "回包/日志/篇数全都对,只有账单多收 —— 第一版实现正是这么漏的。"),

    ("P8-干脆一分不收(反向:多收的对侧)", SRV,
     "        if _bill_user_id and _billable_updated > 0:",
     "        if False:",
     "少了混批那条反向对照,「防御篇不收钱」可以靠「这个接口不收钱了」满足。"),

    # ── ④ playbook 缓存 / TTL / 版本 / 指纹 ────────────────────
    ("P9-每次都重新蒸(缓存形同虚设)", PB,
     "    live = load_live_playbook(conn, brand_id, fingerprint)",
     "    live = None",
     "表还在写,但每批标题都多打一次蒸馏 LLM,而没人看得出来。"),

    ("P10-TTL 失效(过期的照用)", PB,
     '            " WHERE brand_id = %s AND expires_at > now() "',
     '            " WHERE brand_id = %s "',
     "过期不删行是有意的(留痕可回看),所以「不取用」必须靠查询条件保证。"),

    ("P11-换版之后旧 playbook 照用", PB,
     "    if str(row.get(\"version\") or \"\") != PLAYBOOK_VERSION:",
     "    if False:",
     "改完 prompt 之后旧角度还会用到 TTL 到期,而那七天没人能解释产出为什么没变。"),

    ("P12-知识库变了也不失效", PB,
     "    if str(row.get(\"kb_fingerprint\") or \"\") != str(fingerprint or \"\"):",
     "    if False:",
     "客户补了资料,防御写法仍按旧素材来 —— 补资料这个动作就白做了。"),

    # ── ⑤ 正文单点注入 ────────────────────────────────────────
    ("P13-正文侧永远拿不到 playbook", PB,
     '    if str(user_choice or "") != DEFENSIVE_USER_CHOICE:\n        return ""',
     '    if True:\n        return ""',
     "标题按蒸出来的角度写、正文完全不知道有这回事 —— 两边分叉且都不报错。"),

    ("P14-正文块发给所有方向", PB,
     '    if str(user_choice or "") != DEFENSIVE_USER_CHOICE:\n        return ""',
     '    if False:\n        return ""',
     "把公司词写法套到榜单/指南类文章上,而那些文章的商业对象不是品牌自己。"),

    ("P15-正文注入点被摘掉(挂在没人调的地方)", AW,
     "        from .defensive_playbook import render_defensive_body_prompt\n"
     "        system_prompt += render_defensive_body_prompt(",
     "        from .defensive_playbook import render_defensive_body_prompt\n"
     "        _unused_defensive_block = render_defensive_body_prompt(",
     "函数还在、还被调,但结果没拼进 prompt。"
     "🔴 单点判据只数调用点的话这一发会溜过去 —— 它就是来验这件事的。"),

    # ── ⑥ sources_used 的数 ───────────────────────────────────
    ("P16-已引用样本不按八问过滤(命中数被撑大)", PB,
     "    hit = [r for r in rows\n"
     '           if any(tok and tok in str(r.get("question") or "") for tok in question_tokens)]',
     "    hit = list(rows)",
     "「这个品牌全部被引用行」不是「公司词问法下被引用的行」。"),

    ("P17-事实计数改成「这张表有几列」", PB,
     '    used["client_profile_facts"] = len(non_empty)',
     '    used["client_profile_facts"] = len(facts or {})',
     "列数恒定,客户给没给料看不出来 —— 一个恒定的数解释不了任何事。"),

    # ── ⑦ fail-soft ───────────────────────────────────────────
    ("P18-蒸馏挂了就抛(标题跟着断)", PB,
     '        logger.warning("[#185 playbook] 蒸馏失败(标题仍出,落模板): %r", exc)\n'
     '        return None, "distill_failed: %s" % (str(exc)[:120],)',
     '        logger.warning("[#185 playbook] 蒸馏失败(标题仍出,落模板): %r", exc)\n'
     "        raise",
     "playbook 只是一份写法参考,蒸不出来不该让整批标题生成断掉。"),

    ("P19-缺 playbook 时不给原因(静默 None)", PB,
     '    return {"version": None, "reason": str(reason or "unknown")}',
     '    return {"version": None, "reason": ""}',
     "前端会把它显示成「一切正常」,而 AI 那一层根本没参与。"),
]


def _no_control_chars():
    """仪器自检:本脚本与判据包里不许有裸控制字符。

    🔴 本仓 09-13 栽过:heredoc 把 `\\b` 吃成 0x08 写进了判据,
       于是一条否定断言恒绿。
    """
    bad = []
    targets = [os.path.join("scripts", "poison_185_c3.py")]
    pkgdir = os.path.join(ROOT, PKG)
    for fn in sorted(os.listdir(pkgdir)):
        if fn.endswith(".py"):
            targets.append(PKG + "/" + fn)
    for rel in targets:
        text = read(rel)
        hits = sorted({hex(ord(ch)) for ch in text
                       if ord(ch) < 32 and ch not in "\n\r\t"})
        if hits:
            bad.append((rel, hits))
    return bad


def main():
    bad = _no_control_chars()
    if bad:
        print("  !! 仪器自己带控制字符,先修仪器:")
        for f, hits in bad:
            print("     %s %s" % (f, hits))
        return 2

    paths = sorted({p for _, p, _, _, _ in POISONS})
    base_sha = {p: sha(p) for p in paths}
    base_src = {p: read(p) for p in paths}

    failed, total = run()
    print("基线: 收集 %d 条 / 失败集 %d 条" % (total, len(failed)))
    if failed:
        print("  !! 基线失败集非空 —— 红基线会让每一发毒都像命中")
        for f in sorted(failed):
            print("     " + f)
        return 2
    if total != EXPECTED_N:
        print("  !! 收集到 %d 条,预期 %d 条" % (total, EXPECTED_N))
        return 2

    # 可选:`python scripts/poison_185_c3.py P18 NC` 只复跑点名的那几发。
    # 🔴 子集跑出来的结论**只对那几发有效**;报数时要说清是子集,
    #    别拿"子集 2/2 红"去替代全表。
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    selected = [p for p in POISONS
                if not only or any(p[0].startswith(k) for k in only)]
    if only:
        print("子集模式:只跑 %d/%d 发 —— %s"
              % (len(selected), len(POISONS), ", ".join(p[0][:14] for p in selected)))

    rows = []
    for name, path, old, new, why in selected:
        src = base_src[path]
        cnt = src.count(old)
        if cnt != 1:
            rows.append((name, "毒没下成", "锚命中 %d 次(要 1 次)" % cnt))
            continue
        write(path, src.replace(old, new, 1))
        psha = sha(path)
        if psha == base_sha[path]:
            write(path, base_src[path])
            rows.append((name, "毒没下成", "sha 没变"))
            continue
        try:
            pf, ptotal = run()
        finally:
            write(path, base_src[path])
            assert sha(path) == base_sha[path], "还原没回到基线 sha!"
        if name.startswith("NC-"):
            verdict = "绿(阴性对照 ✓)" if not pf else "红(!! 阴性对照不该红)"
        else:
            verdict = "红(锁有牙)" if pf else "绿(没牙/够不着/冗余)"
        rows.append((name, verdict, "sha %s->%s · 收集 %d · 抓住它的: %s" % (
            base_sha[path], psha, ptotal,
            ", ".join(sorted(x.split("::")[-1] for x in pf))[:110] or "无")))

    print()
    for name, verdict, detail in rows:
        print("%-48s %-22s %s" % (name, verdict, detail))
    f2, t2 = run()
    print("\n还原后复跑: 收集 %d / 失败 %d" % (t2, len(f2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
