# -*- coding: utf-8 -*-
"""WO_206 c1b①' 注毒闸 —— 专打**新加的那几把锁**。

Review 09-14 复核 c1a 时打出的三个缺口,每个在这里都有一发对应的毒:
  · P5  文件级 blanket 自动收编新字面量 ⇒ 旧版「每条已分类」不红
  · P9  in-sync 判据先覆盖签入表再比对 ⇒ census 坏掉时把表冲空还报绿
  · Q3  百炼侧只冻计数 ⇒ 改一个百炼名,计数不变、内容变了,锁不红
外加 c1a 的原病:枚举锚被改窄 ⇒ 分母悄悄缩水,而当时一条判据都没红。
"""
import glob
import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/deepseek_official_line_2026_09_14"
#: c1p 的计价包与主线锁包一起跑 —— 毒可能只打红其中一个,
#: 只跑一个的话另一个的红会被当成"没这回事"。
PKG2 = "tests/deepseek_pricing_bands_2026_09_14"
EXPECTED_N = 49
WHITESPACE = chr(10) + chr(13) + chr(9)

CEN = PKG + "/census.py"
CLS = PKG + "/classified.py"
CST = "config/deepseek_models.py"
EMP = "employees/base_employee.py"
ADV = "advisors/base_advisor.py"
LLU = "writing/llm_utils.py"
WSR = "services/writing_style_reviewer.py"
SET = "config/settings_manager.py"
MDC = "config/model_config.py"
STY = "writing/style_registry.py"
MAV = "services/multi_ai_voter.py"
TST = PKG + "/test_emit_site_census.py"
TRK = "tools/llm_call_tracker.py"

ENV = dict(os.environ)
ENV["TEST_DATABASE_URL"] = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_196_test"
ENV["PYTHONIOENCODING"] = "utf-8"


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def read(path):
    return io.open(os.path.join(ROOT, path), encoding="utf-8").read()


def write(path, text):
    io.open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="\n").write(text)


def run():
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, PKG2, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    failed = set()
    for line in out.splitlines():
        # 只认短摘要行,nodeid 必带 `::`(pytest 的 captured-log 也以 ERROR 开头)
        if not (line.startswith("FAILED ") or line.startswith("ERROR ")):
            continue
        parts = line.split()
        if len(parts) < 2 or "::" not in parts[1]:
            continue
        failed.add(parts[1].split(" - ")[0])
    c = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, PKG2, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    total = len([l for l in c.stdout.decode("utf-8", "replace").splitlines()
                 if "::" in l])
    return failed, total


POISONS = [
    # 🔴 **阴性对照**(唯一一发应当保持绿的)。全红的毒表证明不了判据有区分力 ——
    #    它同样可能是"随便改点什么都红"。这一发只加一句无害注释。
    ("NC-阴性对照:分类表里加一句无害注释(应当**绿**)", CLS,
     "FILE_VERDICT = {",
     "# 阴性对照注入的一行,无行为影响\nFILE_VERDICT = {",
     "字节变了、行为没变 —— 绿才对。红了说明有判据在钉形状不钉行为。"),

    # ── Review 的三个缺口 ──────────────────────────────────────
    ("P5-常量模块偷加一个新名(blanket 自动收编)", CST,
     '    "deepseek-reasoner",\n})',
     '    "deepseek-reasoner",\n    "deepseek-sneaky-newname",\n})',
     "旧版只有 in-sync 会红,而它的提示语引向「重新渲染」——"
     "照做就把新名静默收进表里。blanket 预算红了才叫拦住。"),

    ("P9-census 瞎掉(锚匹配不到任何东西)", CEN,
     "_MODEL_LITERAL = re.compile(\n"
     "    r'[\"\\'](deepseek[-/][A-Za-z0-9._/\\-]*)[\"\\']', re.IGNORECASE)",
     "_MODEL_LITERAL = re.compile(\n"
     "    r'[\"\\'](zzz-never-matches-anything)[\"\\']', re.IGNORECASE)",
     "仪器瞎了。旧版 in-sync 会先把签入表冲成空的、再和空的比对,然后**报绿**。"),

    ("Q3-改一处百炼名(计数不变、内容变了)", EMP,
     '    "deepseek-v4-flash": {',
     '    "deepseek-flash": {',
     "Review 实测过:只冻计数的反向对照全程是绿的。"),

    # ── c1a 的原病:枚举锚被改窄 ────────────────────────────────
    ("A1-枚举锚收窄回「只认经典发出写法」", CEN,
     "            found = [(m.start(), m.group(1)) for m in _MODEL_LITERAL.finditer(line)]",
     "            found = [(m.start(), n) for n in classic\n"
     "                     for m in [_CLASSIC_EMIT_SHAPE.search(line)] if m]",
     "这正是 c1a 栽的那一跤:分母从 213 缩到 135,而当时**一条判据都没红**。"),

    ("A2-常量引用不算进分母(改到最后表会自己空掉)", CEN,
     "            if rel.replace(\"\\\\\", \"/\") != _CONST_HOME and not _is_import(line):",
     "            if False:",
     "字面量换成常量就从分母里消失 ⇒ 本单改得越多、尺子越短,最后空表全绿。"
     " 🔴 c1b①' 阶段它**够不着**(生产里还没有一处引用常量),当时记的是绿;"
     "c1b② 换了 9 处之后它就可达了 —— 这一发的红正是「分母没随改动融化」的验收。"),

    # ── 新锁各自的验收 ────────────────────────────────────────
    ("S1-把一条百炼行标成 switch(拿的不是官方历史名)", CLS,
     '"db/monitoring_db.py": ("emit", "dashscope", "a", "元组第一位显式写 dashscope,模型名是百炼侧 ID", "keep")',
     '"db/monitoring_db.py": ("emit", "dashscope", "a", "元组第一位显式写 dashscope,模型名是百炼侧 ID", "switch")',
     "switch 的含义是「把旧名归一到新名」,前提是它现在拿的确实是官方旧名。"),

    # ── c1b② 那批改动各自的验收 ────────────────────────────────
    ("B1-把写作链默认写回旧名(改好的又改回去)", LLU,
     "        default_model = DEEPSEEK_OFFICIAL_FLASH",
     '        default_model = "deepseek-v4-flash"',
     "旧名今天仍然调得通,不会报错 —— 只有思考开关不认新名、默认开思考(慢约 17% 贵约 35%)。"),

    ("B2-评审A 默认写回 deepseek-v3.2(P1 复发)", WSR,
     '         "model": os.getenv("REVIEW_MODEL_A", DEEPSEEK_OFFICIAL_FLASH)},',
     '         "model": os.getenv("REVIEW_MODEL_A", "deepseek-v3.2")},',
     "v3.2 在官方线返 400 ⇒ 评审A 恒失败 ⇒ 恒单评审 ⇒ replace 那条路又不通了。"),

    # ── Review 09-14 点名的四个缺口(原先**只有 in-sync 会红**,而 in-sync 的
    #    提示语引向「重新渲染」—— 照做就全绿了)。这四发必须被别的锁抓住。
    ("R2-把已改行升成 v4-pro(升档冒充改名)", SET,
     '        "geo_scoring": {"provider": "deepseek", "model": DEEPSEEK_OFFICIAL_FLASH},',
     '        "geo_scoring": {"provider": "deepseek", "model": "deepseek-v4-pro"},',
     "v4-pro 不是 flash 的新名,是**另一档**:贵约 3 倍、还开思考。"
     "旧版 done 判定用 `in EMITTABLE`,于是它被当成「改好了」。"
     "🔴 用字面量不用 PRO 常量:那个文件没 import 它,用常量会以 NameError 转红 ——"
     "那是「为错误的原因红」,证明不了锁有牙。"),

    ("R6-把还没改的行直接写成 v4-pro(done 计数被撑大)", MDC,
     '            "model_name": DEEPSEEK_OFFICIAL_FLASH,\n'
     '            "api_key": DEEPSEEK_CONFIG["api_key"],\n'
     '            "client_args": {\n'
     '                "base_url": DEEPSEEK_CONFIG["base_url"],\n'
     '            },\n'
     '        },\n'
     '        {\n'
     '            # config_name 是**注册表键**',
     '            "model_name": "deepseek-v4-pro",\n'
     '            "api_key": DEEPSEEK_CONFIG["api_key"],\n'
     '            "client_args": {\n'
     '                "base_url": DEEPSEEK_CONFIG["base_url"],\n'
     '            },\n'
     '        },\n'
     '        {\n'
     '            # config_name 是**注册表键**',
     "同上:PRO 永不算 done,否则「已改条数」可以靠升档刷上去。"),

    ("R5-已改行上再塞回一个旧别名字面量", STY,
     '         "models": [DEEPSEEK_OFFICIAL_FLASH]},',
     '         "models": [DEEPSEEK_OFFICIAL_FLASH, "deepseek-reasoner"]},',
     "新字面量继承那一行的 switch 判决,于是被当成「待改的旧名」放行 ——"
     "而它其实是刚被塞回去的。"),

    ("R8-把官方线 keep 的 v4-pro 改成 flash(降档)", MAV,
     '        "model": "deepseek-v4-pro",',
     '        "model": DEEPSEEK_OFFICIAL_FLASH,',
     "v4-pro 是官方今天**还在供应**的另一档,是 Social-CTO 老板拍板刻意选的;"
     "改成 flash 是降档,贵/慢/质量三件事一起变,而原先只有 in-sync 会红。"),

    # ── c1d 总锁的验收 ────────────────────────────────────────
    # 🔴 c1e 之后例外表是**空的**,所以这一发改成「往空表里塞一条」。
    #    锚跟着被测对象走 —— 原来的锚(那唯一一条例外)已经不存在了,
    #    留着它只会报「毒没下成」,而那读起来很像"这里没事"。
    ("D1-往空的例外表里塞一条已经改好的行(许可证只增不减)", TST,
     "OFFICIAL_A_EMIT_EXCEPTIONS = {\n",
     "OFFICIAL_A_EMIT_EXCEPTIONS = {\n"
     '    ("writing/llm_utils.py", "deepseek-flash"):\n'
     '        "毒:这一行其实早就改好了,例外表却还写着它有理由不改",\n',
     "例外表会变成一张只增不减的许可证 —— 那一行早就改好了,表上仍写着"
     "「它有理由不改」,下一个人照抄这条理由去豁免别的行。"),

    # ── c1p' · Review 实测的缺口 C8:A 调 B 的**那一行**没人验 ──────────
    ("C8-上下文算钱时不传调用时刻(Review 缺口)", TRK,
     "            at=self.started_at_utc,",
     "            at=None,",
     "把 `at=self.started_at_utc` 摘成 `at=None` ⇒ 所有调用一律按高峰计。"
     "原来整包**全绿** —— 因为那条判据只钉了「属性存在」,没钉「它被用来算钱」。"),

    ("U1-社媒侧多出一个没人读过的名字", ADV,
     '        "default_model": "deepseek-chat",',
     '        "default_model": "deepseek-chat",\n        "_poison_model": "deepseek-v4-pro",',
     "路径规则会自动给它一个 unread 判决 —— 没有冻结量的话,没人会发现多了一行。"),
]


def _no_control_chars():
    bad = []
    for f in ([__file__] + glob.glob(os.path.join(ROOT, PKG, "*.py"))
              + glob.glob(os.path.join(ROOT, PKG2, "*.py"))):
        t = io.open(f, encoding="utf-8").read()
        hits = [hex(ord(c)) for c in t if ord(c) < 32 and c not in WHITESPACE]
        if hits:
            bad.append((f, sorted(set(hits))))
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
    doc = "docs/AI-CONTEXT/DEEPSEEK_OFFICIAL_EMIT_SITES_2026-09-14.md"
    doc_sha = sha(doc)

    failed, total = run()
    print("基线: 收集 %d 条 / 失败集 %d 条" % (total, len(failed)))
    if failed:
        print("  !! 基线失败集非空")
        for f in sorted(failed):
            print("     " + f)
        return 2
    if total != EXPECTED_N:
        print("  !! 收集到 %d 条,预期 %d 条" % (total, EXPECTED_N))
        return 2

    rows = []
    for name, path, old, new, why in POISONS:
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
        pf, ptotal = run()
        # 🔴 每一发都核一遍**签入表有没有被判据改写**。Review 的 P9 正是这条:
        #    判据"先渲染再比对"时,它会在不声不响里把证据文件改掉。
        touched = "" if sha(doc) == doc_sha else "  🔴 签入表被改写了!"
        write(path, base_src[path])
        assert sha(path) == base_sha[path], "还原没回到基线 sha!"
        if name.startswith("NC-"):
            verdict = "绿(阴性对照 ✓)" if not pf else "红(!! 阴性对照不该红)"
        else:
            verdict = "红(锁有牙)" if pf else "绿(没牙/够不着/冗余)"
        rows.append((name, verdict, "sha %s->%s · 收集 %d%s · 抓住它的: %s" % (
            base_sha[path], psha, ptotal, touched,
            ", ".join(sorted(x.split("::")[-1] for x in pf))[:120] or "无")))

    print()
    for name, verdict, detail in rows:
        print("%-46s %-22s %s" % (name, verdict, detail))
    f2, t2 = run()
    print("\n还原后复跑: 收集 %d / 失败 %d · 签入表 sha %s"
          % (t2, len(f2), "未被动过" if sha(doc) == doc_sha else "🔴 被改写了"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
