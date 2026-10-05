# -*- coding: utf-8 -*-
"""WO_217-c1a 注毒台:证明这 11 条判据**有牙**。

规矩(本仓踩出来的):
  · 基线失败集必须为空,且 collected 必须等于 EXPECTED_N —— 「零 FAILED 行」
    既可能是全绿,也可能是根本没跑。
  · 每发毒的锚必须**命中且只命中一次**;命中 0 次 = 毒没下成,判绿无意义。
  · 下毒后 sha 必须变;还原后 sha 必须回到基线值。
  · 比**失败集差分**,不比条数。
  · 负样本(无害注释)必须读绿 —— 否则说明判据在对无关改动报警。
"""
import hashlib
import io
import os
import re
import subprocess
import sys

#: 🔴 Windows 控制台默认 GBK,打不出 🔴 这种字符会当场 UnicodeEncodeError。
#:   而崩在「下毒之后、还原之前」会把毒**留在树里**(本仓已有先例)。
#:   所以既强制 UTF-8 输出,下面也用 try/finally 兜底还原。
try:
    sys.stdout.reconfigure(encoding=chr(117)+chr(116)+chr(102)+chr(45)+chr(56), errors=chr(114)+chr(101)+chr(112)+chr(108)+chr(97)+chr(99)+chr(101))
except Exception:
    pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PKG = "tests/model_line_flash_census_2026_09_15"
EXPECTED_N = 13

CENSUS = PKG + "/census.py"
CLASSIFIED = PKG + "/classified.py"
MODELCFG = "config/model_config.py"

#: (名字, 文件, 锚, 换成什么, 一句话说它模拟的是什么退化)
POISONS = [
    ("NC 无害注释", CENSUS,
     "import subprocess",
     "import subprocess  # 无害注释:不改变任何行为",
     "负样本 —— 必须读绿"),

    ("N1 锚缩回旧写法(杀掉 qwen3.x 族)", CENSUS,
     "r')[0-9]*(?:\\.[0-9]+)?[-./][A-Za-z0-9][A-Za-z0-9._/\\-]*)[\"\\']',",
     "r')[-.][A-Za-z0-9._/\\-]*)[\"\\']',",
     "重演本单真实事故:厂商名后要求紧跟分隔符 ⇒ qwen3.7-max 整族看不见,而表看起来是满的"),

    ("N2 摘掉 M 腿(注册表键)", CENSUS,
     "            if reg_re is not None:",
     "            if False:",
     "第三套命名失明 ⇒ server.py:15232 的写文章默认档掉出分母"),

    ("N3 撤掉一条签字", CLASSIFIED,
     '    "kimi_search": ("ROUTE_KEY", "ai_tester.py:606 通道名"),',
     "",
     "校准腿列出的名字没人签字 ⇒ 必须红,不许沉默"),

    #: 🔴 [c1a'] N4 的锚换过一次:原来钉 qwen3-vl-rerank 那行,而 c1a' 恰好改了它
    #:   的理由文字 ⇒ 锚命中 0 次。注毒台当时**没有报假绿**,而是明说「毒没下成,不判」——
    #:   这正是「锚必须命中且只命中一次」那条守卫存在的理由。
    #:   改钉一条本单没动过的行,并把毒串写够长:否则先撞上「理由太短」那条断言,
    #:   打不到真正想验的「不许写其它」。
    ("N4 例外理由写成「其它」", CLASSIFIED,
     '    "qwen-audio-asr":             "语音识别(计价表行)—— 同上",',
     '    "qwen-audio-asr":             "其它原因,反正 DeepSeek 不合适,先这样",',
     "例外没说清 DeepSeek 缺哪项能力 —— 这张表要交 Owner 过目"),

    ("N5 给死配置加一个真调用点", MODELCFG,
     "def get_agentscope_model_configs() -> list[dict]:",
     "def _resurrect():\n    return get_model_for_task('geo_article')\n\n\n"
     "def get_agentscope_model_configs() -> list[dict]:",
     "死配置复活 ⇒ 那 24 个任务键不再是死的,c1b 范围要重算"),

    ("N7 排除集混进一个无关路径", CENSUS,
     '    "scripts/poison_217_c1a.py",                   # 本包的注毒台',
     '    "scripts/poison_217_c1a.py",  # 本包的注毒台' + chr(10) +
     '    "services/",',
     "排除集是天然的藏东西的地方 —— 混进无关路径必须当场红"),

    ("N8 仪器把自己数进分母", CENSUS,
     "            if l.strip() and not is_instrument(l)]",
     "            if l.strip()]",
     "重演复审抓到的那个缺陷:量尺把自己量了进去 ⇒ total_all 1085→1117"),

    ("N6 普查器返回空表", CENSUS,
     "    return rows",
     "    return []",
     "仪器整体死亡 —— 空表本身是**全绿**的,必须有东西喊出来"),
]


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def run():
    """跑一遍,返回 (collected, failed_set)。"""
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_admin:testpw@127.0.0.1:55492/geo_c14_205_test")
    p = subprocess.run([sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
                        "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, env=env)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    failed = set(re.findall(r"^FAILED [^:]+::(\S+)", out, re.M))
    m = re.search(r"(\d+) (?:passed|failed)", out)
    collected = 0
    for mm in re.finditer(r"(\d+) (passed|failed|error)", out):
        collected += int(mm.group(1))
    return collected, failed, out


def main():
    print("=" * 78)
    base_c, base_f, base_out = run()
    print("基线: collected=%d  failed=%s" % (base_c, sorted(base_f) or "空"))
    if base_c != EXPECTED_N:
        print("🔴 基线条数 %d != EXPECTED_N %d —— 仪器没跑满,后面全部作废" % (base_c, EXPECTED_N))
        print(base_out[-1500:])
        return 2
    if base_f:
        print("🔴 基线不是全绿,红基线会让每一发毒都像命中")
        return 2

    results = []
    for name, rel, anchor, repl, why in POISONS:
        full = os.path.join(ROOT, rel)
        #: 🔴 二进制读写。文本模式在 Windows 会把 LF 回写成 CRLF ——
        #:   还原后 sha 对不上基线,而那看起来像「注毒把文件改坏了」。
        #:   本仓 windows-newline-poisons-any-rewrite-tool,本单又踩一次。
        raw = io.open(full, "rb").read()
        src = raw.decode("utf-8")
        hits = src.count(anchor)
        before = sha(rel)
        if hits != 1:
            print("  %-34s 🔴 锚命中 %d 次(要 1)—— 毒没下成,不判" % (name, hits))
            results.append((name, "ANCHOR_MISS", set()))
            continue
        io.open(full, "wb").write(src.replace(anchor, repl).encode("utf-8"))
        after = sha(rel)
        assert after != before, "下毒后 sha 没变"
        try:
            c, f, _ = run()
        finally:
            io.open(full, "wb").write(raw)
        assert sha(rel) == before, "还原后 sha 对不上基线"
        new = f - base_f
        #: 负样本的期望是**绿**,正样本的期望是**红**。
        #:   第一版对两者打同一个标签,于是 NC 读绿被印成「🔴 仍绿」——
        #:   回显在对的结果上报警,读表的人会往错的方向查。
        is_nc = name.startswith("NC")
        ok = (not new) if is_nc else bool(new)
        verdict = (("绿 ✓" if is_nc else "红 ✓") if ok
                   else ("🔴 负样本读红" if is_nc else "🔴 仍绿"))
        print("  %-34s %-8s 新红 %d 条  %s" % (name, verdict, len(new),
                                              ",".join(sorted(new))[:64]))
        results.append((name, verdict, new))

    print("-" * 78)
    nc = [r for r in results if r[0].startswith("NC")]
    bad = [r for r in results if not r[0].startswith("NC") and r[1] != "红 ✓"]
    if nc and nc[0][2]:
        print("🔴 负样本读红了 —— 判据在对无关改动报警")
        return 1
    if bad:
        print("🔴 这些毒没被抓住:%s" % [r[0] for r in bad])
        return 1
    print("✅ %d 发毒全部被抓 · 负样本绿" % len([r for r in results if not r[0].startswith("NC")]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
