# -*- coding: utf-8 -*-
"""WO_211 c1 注毒闸。

这是一次**放开权限**的改动 —— 毒要打在"会不会顺手放开别的闸"上,
不只打在"普通用户能不能用"上。
"""
import glob
import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/research_open_to_users_2026_09_14"
EXPECTED_N = 17
WHITESPACE = chr(10) + chr(13) + chr(9)

API = "api/research_selfserve_api.py"
BA = "auth/brand_access.py"

ENV = dict(os.environ)
ENV["TEST_DATABASE_URL"] = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_211_test"
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
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            failed.add(line.split(" ")[1].split(" - ")[0])
    c = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    total = len([l for l in c.stdout.decode("utf-8", "replace").splitlines()
                 if "::" in l])
    return failed, total


POISONS = [
    ("Q1-把 agent_level 闸装回去(WO 点名)", API,
     '    return {"user_id": int(uid), "is_admin": bool(user.get("is_admin"))}',
     '    if not user.get("is_admin"):\n'
     '        raise HTTPException(status_code=403,\n'
     '                            detail={"code": "NOT_AGENT", "message": "仅服务方可使用自助调研"})\n'
     '    return {"user_id": int(uid), "is_admin": bool(user.get("is_admin"))}',
     "普通用户又被挡回去 —— Owner 点名要开的就是这一格。"),

    # 🔴 这一发换过。上一版用 `or {"user_id": 0}` —— 0 是**假值**,第二道 uid 检查
    #    照样抛 401,于是毒空转、绿得毫无意义(注毒仍绿的第四解:目标是冗余的)。
    #    换成真值才真的把未登录放进来。
    ("Q2-未登录当成真实用户放行", API,
     '    user = getattr(request.state, "user", None)\n'
     '    if not user:\n'
     '        raise HTTPException(status_code=401, detail="请先登录")',
     '    user = getattr(request.state, "user", None) or {"user_id": 999, "is_admin": False}',
     "客户 portal / 公开 token 也能调 —— 放开身份闸顺手放开了登录闸。"),

    ("Q3-所有人当 admin", API,
     '    return {"user_id": int(uid), "is_admin": bool(user.get("is_admin"))}',
     '    return {"user_id": int(uid), "is_admin": True}',
     "读端点的 admin 旁路对所有人打开 ⇒ 谁都能读别人的任务。"),

    ("Q4-写端点不再校验品牌(调用点)", API,
     "    _verify_brand_owner(request, req.brand_id)  # [GEO-R2-CAN-038] 共享 RBAC(含分配客户)\n"
     "\n"
     "    resolved = await preview_resolve(req.industry or \"\")",
     "    resolved = await preview_resolve(req.industry or \"\")",
     "普通用户能对**别人的客户**下单。R3 那几条直接调 helper,删调用点它们照样绿 ——"
     "这一发专打那个缝。"),

    ("Q5-品牌拒绝码改 403", BA,
     '    logger.warning(\n'
     '        f"品牌访问被拒绝: user={user.get(\'username\')} user_id={user_id} "\n'
     '        f"requested brand_id={brand_id}, allowed={allowed_brands}"\n'
     '    )\n'
     '    raise HTTPException(status_code=404, detail="资源不存在")',
     '    logger.warning(\n'
     '        f"品牌访问被拒绝: user={user.get(\'username\')} user_id={user_id} "\n'
     '        f"requested brand_id={brand_id}, allowed={allowed_brands}"\n'
     '    )\n'
     '    raise HTTPException(status_code=403, detail="无权访问该品牌")',
     "403 与 404 分开 = 告诉调用方「这个品牌存在但不归你」,跨租户探测存在性。"),

    ("Q6-读端点不再判属主", API,
     '    if not task or (not actor["is_admin"] and int(task.get("user_id") or 0) != user_id):\n'
     '        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "任务不存在"})',
     '    if not task:\n'
     '        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": "任务不存在"})',
     "放开身份闸之后,越权读会悄悄成立 —— 原来「非服务商进不来」掩盖着这一格。"),
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
        print("  !! 基线失败集非空,先修基线")
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
        verdict = "红(锁有牙)" if pf else "绿(没牙/够不着/冗余)"
        rows.append((name, verdict, "sha %s->%s · 收集 %d · 抓住它的: %s" % (
            base_sha[path], psha, ptotal,
            ", ".join(sorted(x.split("::")[-1] for x in pf))[:150] or "无")))

    print()
    for name, verdict, detail in rows:
        print("%-38s %-22s %s" % (name, verdict, detail))
    f2, t2 = run()
    print("\n还原后复跑: 收集 %d / 失败 %d" % (t2, len(f2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
