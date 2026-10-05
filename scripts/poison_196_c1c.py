# -*- coding: utf-8 -*-
"""#196 c1c 注毒闸。

纪律(每条都对应一次踩过的坑):
  · 基线失败集必须为空,且判据条数必须等于预期 —— 否则「每发毒都像命中」;
  · 每发毒必须**自证字节变了**(sha 前后不同),否则「毒没下成」与「锁没牙」同形;
  · 还原用**字节拷回**,不用 `git checkout --`(那回的是 HEAD,会冲掉同文件里
    尚未提交的其它改动);还原后 sha 必须回到基线值;
  · 判读比**失败集差**,不比 rc。
"""
import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/geo_imgnote_artifact_reason_2026_09_13"
EXPECTED_N = 28
WHITESPACE = chr(10) + chr(13) + chr(9)

API = "api/geo_image_note_api.py"
PRIV = "services/publish_channel_privacy.py"

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
    """返回 (失败集, 收集到的条数)。"""
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    failed, total = set(), 0
    for line in out.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            failed.add(line.split(" ")[1].split(" - ")[0])
        if " passed" in line or " failed" in line:
            for tok in line.replace(",", " ").split():
                pass
    # 条数从 -q 的点行/汇总行取:直接再跑一次 --collect-only 更稳
    c = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    ctext = c.stdout.decode("utf-8", "replace")
    total = len([l for l in ctext.splitlines() if "::" in l])
    return failed, total, out


POISONS = [
    # (名字, 文件, 原文, 毒, 这发毒在验什么)
    ("G-删掉最后那道 contains_vendor_trace", API,
     "if not human or contains_vendor_trace(human):",
     "if not human:",
     "Review 说它是冗余守卫。c1c 给它接上了真正护得住的那两条路,应当转红。"),

    ("P1-白名单退化成只查 ://", API,
     'ct("[A-Za-z]{4,}|://|[=()<>]")',
     'ct("://")',
     "退化后裸异常串与带括号的技术串都原样透出。"),

    ("P1a-只砍掉「4 连字母」那一半", API,
     'ct("[A-Za-z]{4,}|://|[=()<>]")',
     'ct("://|[=()<>]")',
     "白名单两半各自承重;少了字母那半,ConnectionResetError 一类透出。"),

    ("P1b-只砍掉符号那一半", API,
     'ct("[A-Za-z]{4,}|://|[=()<>]")',
     'ct("[A-Za-z]{4,}|://")',
     "少了符号那半,「余额不足 (0)」一类透出。"),

    ("P2-通用句按 state 写反", API,
     '''    "failed": "素材准备失败,收起再展开面板会重新准备",
    "unknown": ("上传时连接中断,渠道可能已收到部分素材,"
                "系统不会自动重传,请联系人工核对"),''',
     '''    "unknown": "素材准备失败,收起再展开面板会重新准备",
    "failed": ("上传时连接中断,渠道可能已收到部分素材,"
               "系统不会自动重传,请联系人工核对"),''',
     "写反 = 叫用户去重传一个渠道侧已有残留的作品。"),

    ("P3-去掉词表外主机脱敏", API,
     "cleaned = scrub_hosts_and_urls(scrub_text(text)).strip()",
     "cleaned = scrub_text(text).strip()",
     "词表外的 x.cn 一类主机名会原样漏出去。"),

    ("P4-调用点不传真 state(恒按 unknown)", API,
     "human = _humanize_artifact_reason(raw, state=state)",
     'human = _humanize_artifact_reason(raw, state="unknown")',
     "接线断了:两个 state 会拿到同一句。"),

    ("P5-形状脱敏只吃 URL 不吃裸主机名", PRIV,
     "    value = _URL_RE.sub(NEUTRAL_CHANNEL_LABEL, value)\n"
     "    return _HOSTNAME_RE.sub(NEUTRAL_CHANNEL_LABEL, value)",
     "    return _URL_RE.sub(NEUTRAL_CHANNEL_LABEL, value)",
     "裸主机名(没有 scheme)是适配器报错里最常见的形态。"),

    ("K-主机名边界退回裸词边界(Review c1c 未钉)", PRIV,
     "(?<![A-Za-z0-9.\\-])(?:[a-z0-9\\-]+\\.)+[a-z]{2,}(?![A-Za-z0-9.\\-])",
     "\\b(?:[a-z0-9\\-]+\\.)+[a-z]{2,}\\b",
     "中文紧贴主机名时词边界不成立(中文也算词字符),x.cn 原样漏出。"),

    ("M-字母阈值 4->5(Review c1c 未钉)", API,
     're.compile(r"[A-Za-z]{4,}|://|[=()<>]")',
     're.compile(r"[A-Za-z]{5,}|://|[=()<>]")',
     "松一格就放行 host / port / null / json 这类 4 字母技术词。"),

]



def _no_control_chars():
    """仪器自检:注毒脚本与判据文件里不许出现控制字符。

    今天三次同族事故:heredoc / 非 raw 字符串把 `反斜杠b` 吃成退格 0x08。
    在判据的正则里它会让否定断言恒真恒绿;在这里它只是让报表读不出来 ——
    但两者是同一个病,所以闸放在同一个地方。
    """
    import glob
    bad = []
    for f in [__file__] + glob.glob(os.path.join(ROOT, PKG, "*.py")):
        t = io.open(f, encoding="utf-8").read()
        hits = [hex(ord(c)) for c in t
                if ord(c) < 32 and c not in WHITESPACE]
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

    base_sha = {API: sha(API), PRIV: sha(PRIV)}
    base_src = {API: read(API), PRIV: read(PRIV)}

    failed, total, out = run()
    print("基线: 收集 %d 条 / 失败集 %d 条" % (total, len(failed)))
    if failed:
        print("  !! 基线失败集非空,先修基线 —— 否则每发毒都会'看起来命中'")
        for f in sorted(failed):
            print("     " + f)
        return 2
    if total != EXPECTED_N:
        print("  !! 收集到 %d 条,预期 %d 条 —— 仪器没跑全" % (total, EXPECTED_N))
        return 2

    rows = []
    for name, path, old, new, why in POISONS:
        src = base_src[path]
        # 白名单那条在源码里是 re.compile(...),这里用简写占位再还原
        o = old.replace('ct("', 're.compile(r"').replace('")', '")') if old.startswith("ct(") else old
        n = new.replace('ct("', 're.compile(r"').replace('")', '")') if new.startswith("ct(") else new
        cnt = src.count(o)
        if cnt != 1:
            rows.append((name, "毒没下成", "锚命中 %d 次(要 1 次)" % cnt))
            continue
        write(path, src.replace(o, n, 1))
        poisoned_sha = sha(path)
        if poisoned_sha == base_sha[path]:
            write(path, base_src[path])
            rows.append((name, "毒没下成", "sha 没变"))
            continue
        pf, ptotal, pout = run()
        write(path, base_src[path])
        assert sha(path) == base_sha[path], "还原没回到基线 sha!"
        verdict = "红(锁有牙)" if pf else "绿(没牙 / 够不着 / 冗余)"
        rows.append((name, verdict,
                     "sha %s->%s · 收集 %d · 抓住它的: %s"
                     % (base_sha[path], poisoned_sha, ptotal,
                        ", ".join(sorted(x.split("::")[-1] for x in pf))[:180] or "无")))

    print()
    for name, verdict, detail in rows:
        print("%-40s %-24s %s" % (name, verdict, detail))
    # 还原后再跑一遍,证明树回到了基线
    f2, t2, _ = run()
    print("\n还原后复跑: 收集 %d / 失败 %d" % (t2, len(f2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
