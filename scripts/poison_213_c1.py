# -*- coding: utf-8 -*-
"""WO_213 c1 注毒闸。"""
import glob
import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/pipeline_kill_switch_2026_09_14"
EXPECTED_N = 14
WHITESPACE = chr(10) + chr(13) + chr(9)

DY = "api/geo_douyin_api.py"
IN = "api/geo_image_note_api.py"
CW = "services/geo_douyin/contract_worker.py"
GT = "services/geo_douyin/pipeline_gate.py"
SC = "scheduler.py"
SV = "server.py"

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
        [sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
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
        [sys.executable, "-m", "pytest", PKG, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    total = len([l for l in c.stdout.decode("utf-8", "replace").splitlines()
                 if "::" in l])
    return failed, total


POISONS = [
    # 🔴 **阴性对照**(唯一一发应当保持绿的)。
    #    一张全红的毒表证明不了判据有区分力 —— 它同样可能是"随便改点什么都红"。
    #    这一发在同一个文件的同一处加一句无害注释:字节变了、sha 变了,
    #    但行为没变。它**绿**才说明上面那几发的红是冲着行为去的。
    #    它要是红了,说明有判据在钉"这个文件长什么样"而不是"它做了什么"。
    ("NC-阴性对照:只加一句无害注释(应当**绿**)", IN,
     "router = APIRouter(\n    tags=[\"GEO图文合同链\"],",
     "router = APIRouter(\n    # 阴性对照注入的一行,无行为影响\n    tags=[\"GEO图文合同链\"],",
     "字节变了、行为没变 —— 绿才对。红了说明有判据在钉形状不钉行为。"),

    # 🔴 Review 09-14 复核挖出的缺口 Q2:路由器级依赖护得住「同一个 router 上
    #    新加的路由」,**护不住**「同一个文件里新建的另一个 router」。
    #    c1 那版判据按**名字**取路由器(from … import router),名字看不见邻居 ——
    #    38 条一条不少、router 仍挂着闸,于是整批判据全程是绿的。
    #    c1b 把分母改成「模块里所有 APIRouter 对象」+ AST 数构造次数,这一发就是它的验收。
    ("Q2-同文件新建裸 APIRouter 再挂一条路由(Review 缺口)", IN,
     "    dependencies=[Depends(pipeline_gate)],\n)",
     "    dependencies=[Depends(pipeline_gate)],\n)\n\n"
     "public_router = APIRouter(tags=[\"GEO图文合同链-公开\"])\n\n\n"
     "@public_router.get(\"/api/image-notes/poison-probe\")\n"
     "async def _poison_probe():\n"
     "    return {\"ok\": True}",
     "新 router 不受总闸:闸关了它那几条照样进,而 38 这个数一点没变。"),

    ("K1b-合同链去掉路由器级依赖(Review 点名)", IN,
     "    dependencies=[Depends(pipeline_gate)],\n)",
     ")",
     "合同链 9 条又回到不受闸 —— 包括花供应商钱的 publish-batch。"),

    ("K1c-老链去掉路由器级依赖", DY,
     "    dependencies=[Depends(pipeline_gate)],\n)",
     ")",
     "老链 29 条不受闸。"),

    ("K2-闸关改成放行", GT,
     "    if not is_pipeline_enabled():\n"
     "        raise HTTPException(status_code=503, detail=pipeline_disabled_detail())",
     "    return None",
     "闸形同虚设:关了也照样进。"),

    ("K2b-闸关返 200 而不是 503", GT,
     "        raise HTTPException(status_code=503, detail=pipeline_disabled_detail())",
     "        raise HTTPException(status_code=200, detail=pipeline_disabled_detail())",
     "「功能关了」与「跑通了」在状态码上分不出来 —— 网关/埋点/前端错误分支都看不见。"),

    ("K3a-run_tick 不认闸", CW,
     '    counts["pipeline_open"] = 1 if pipeline_gate_open() else 0',
     '    counts["pipeline_open"] = 1',
     "HTTP 面关了、后台还在领活 —— 最糟的半开状态。"),

    ("K3b-闸关连收敛器一起停(我第一版写错的那样)", CW,
     '    counts["pipeline_open"] = 1 if pipeline_gate_open() else 0\n',
     '    counts["pipeline_open"] = 1 if pipeline_gate_open() else 0\n'
     '    if not counts["pipeline_open"]:\n        return counts\n',
     "关闸那一刻,在途任务连同冻结一起卡死 —— 比「图文还在跑」糟得多。"),

    # Q2 的挂载侧变体:新 router 可能是从**别的文件**搬来的,定义侧那条锁看不见。
    ("Q2b-server.py 把一个不受闸的 router 也挂上去", SV,
     "    from api.geo_image_note_api import router as geo_image_note_router\n"
     "    app.include_router(geo_image_note_router)",
     "    from api.geo_image_note_api import router as geo_image_note_router, "
     "public_router\n"
     "    app.include_router(geo_image_note_router)\n"
     "    app.include_router(public_router)",
     "多挂一条不受闸的链:两个 api 文件本身一个字没动,定义侧的锁全绿。"),

    ("K4-收敛 job 的「故意不认闸」理由被删", SC,
     "    🔴 [WO_213] 这个 job **故意不认总闸**",
     "    🔴 [WO_213] 这个 job 不认总闸",
     "没写理由的例外,下一个人会当成漏加然后补上 —— 而补上就是把在途的卡死。"),
]


def _no_control_chars():
    bad = []
    for f in [__file__] + glob.glob(os.path.join(ROOT, PKG, "*.py")):
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
        write(path, base_src[path])
        assert sha(path) == base_sha[path], "还原没回到基线 sha!"
        # 阴性对照的判读**反过来**:它绿才对。混在一起用同一句话报,
        # 读表的人会把"应当绿的那发绿了"当成一个没牙的锁。
        if name.startswith("NC-"):
            verdict = "绿(阴性对照 ✓)" if not pf else "红(!! 阴性对照不该红)"
        else:
            verdict = "红(锁有牙)" if pf else "绿(没牙/够不着/冗余)"
        rows.append((name, verdict, "sha %s->%s · 收集 %d · 抓住它的: %s" % (
            base_sha[path], psha, ptotal,
            ", ".join(sorted(x.split("::")[-1] for x in pf))[:130] or "无")))

    print()
    for name, verdict, detail in rows:
        print("%-44s %-22s %s" % (name, verdict, detail))
    f2, t2 = run()
    print("\n还原后复跑: 收集 %d / 失败 %d" % (t2, len(f2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
