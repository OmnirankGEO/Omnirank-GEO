"""变异注入验证 · 返修单 `REWORK_T4_SAFEMARKDOWN_IMAGE_2026-07-29` §5。

同上一轮口径：
* 每个变异先跑 **import 冒烟**（语法被写坏 → 判 `BROKEN`，**不计入 KILLED**）；
* pytest 的 **FAILED 与 ERROR 两类分别解析**（只 grep FAILED 会把 collection
  error 读成"零失败"，从而把"锁没咬住"误判成"锁咬住了"）；
* 还原用 `cp` 备份，**不用 `git checkout`**；
* 变异要拆到该锁**真正依赖**的那一层。

覆盖两侧：Python 锁（pytest）+ 前端真渲染锁（Playwright，`naturalWidth`）。
前端变异跑得慢（要 rebuild），所以只在改到前端文件时才跑那条。
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_TEST = "tests/test_owned_image_policy_2026_07_29.py"
FE_LOCK = ["node", "scripts/verify-safeimage-render.mjs"]


@dataclasses.dataclass(frozen=True)
class Mutation:
    mid: str
    layer: str
    path: str
    old: str
    new: str
    expect: str
    frontend: bool = False


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "S1", "渲染层本体：SafeImage 退回无条件文字占位（返修前的形态）",
        "frontend/src/components/SafeMarkdown.tsx",
        "  if (safeSrc && isOwnedImageUrl(safeSrc)) {",
        "  if (false) {",
        "锁1 转红", frontend=True,
    ),
    Mutation(
        # 首轮这条拆的是 path 前缀判定,结果被"段数必须恰好 2"那层独立挡住而存活
        # —— 那是防御纵深,不是漏锁。按返修单 §3「不得放行任意 http(s) 图片」,
        # 锁真正依赖的是"不放行任意 http(s)"这条界,所以拆到这里。
        "S2", "白名单层：放宽成「只要 http(s) 就渲染」（返修单 §3 约束 3 的反面）",
        "frontend/src/lib/ownedImagePolicy.ts",
        "  const url = String(value ?? '')",
        "  if (/^https?:/i.test(String(value ?? ''))) return true\n  const url = String(value ?? '')",
        "锁2/3 转红", frontend=True,
    ),
    Mutation(
        # 首轮写成 `if (false) return false`,协议相对 URL 转头被 path 前缀那层挡住
        # (`//evil…` 不以 `/uploads/` 开头)而存活。直接把这一层判反,才是拆到位。
        "S3", "协议相对层：把 // 开头判成自有（绕过同源判定的经典口子）",
        "frontend/src/lib/ownedImagePolicy.ts",
        "  if (url.startsWith('//')) return false",
        "  if (url.startsWith('//')) return true",
        "锁3 转红", frontend=True,
    ),
    Mutation(
        "S4", "同源判定层：跨源也算自有（外部域名伪造同款路径即可绕过）",
        "frontend/src/lib/ownedImagePolicy.ts",
        "    if (hostOf(parsed.origin) !== hostOf(expected)) return false",
        "    if (false) return false",
        "锁2 转红", frontend=True,
    ),
    Mutation(
        "S5", "后端白名单层：img 的 src 不再判自有，只要是串就放行",
        "services/safe_markdown_renderer.py",
        "        if name == \"src\":\n            return is_owned_image_url(value)",
        "        if name == \"src\":\n            return True",
        "锁5 转红",
    ),
    Mutation(
        "S6", "后端剥离层：img 退回不在白名单（返修前的无条件剥）",
        "services/safe_markdown_renderer.py",
        "    \"img\",\n    \"li\",",
        "    \"li\",",
        "锁5 转红",
    ),
    Mutation(
        "S7", "后端同源层：Python 侧不再判同源",
        "services/owned_image_policy.py",
        "        if _host_of(origin) != _host_of(expected):\n            return False",
        "        if False:\n            return False",
        "共享向量锁转红",
    ),
    Mutation(
        # 同 S2：单拆前缀会被"段数必须恰好 2"那层独立挡住而存活（防御纵深）。
        # 拆到锁真正依赖的那条界：不放行任意 http(s)。
        "S8", "后端白名单层：放宽成「只要 http(s) 就算自有」",
        "services/owned_image_policy.py",
        "    url = str(value or \"\")",
        "    if str(value or \"\").lower().startswith((\"http://\", \"https://\")):\n"
        "        return True\n"
        "    url = str(value or \"\")",
        "共享向量锁 + 跟踪像素锁转红",
    ),
    Mutation(
        # 首轮拆 `..` 守卫，被"段数必须恰好 2"独立挡住而存活
        # （`662/../../../etc/passwd` 是 5 段）。穿越锁真正依赖的是段数那条界。
        "S9", "后端路径段数层：不再要求恰好 <brand>/<file> 两段（穿越与嵌套即可绕过）",
        "services/owned_image_policy.py",
        "    if len(segments) != 2:\n        return False",
        "    segments = [segments[0], segments[-1]] if len(segments) >= 2 else segments\n"
        "    if len(segments) != 2:\n        return False",
        "路径穿越锁转红",
    ),
    Mutation(
        "S10", "无源 img 清理层：bleach 摘掉 src 后的空 <img> 不再删（前后端语义分叉）",
        "services/safe_markdown_renderer.py",
        "    cleaned = _SOURCELESS_IMG_RE.sub(\"\", cleaned)",
        "    cleaned = cleaned",
        "无源 img 锁转红",
    ),
    Mutation(
        "S11", "SSOT 层：前端改成自己抄一份前缀常量（漂移的起点）",
        "frontend/src/lib/ownedImagePolicy.ts",
        "const PATH_PREFIX: string = policy.path_prefix",
        "const PATH_PREFIX: string = '/uploads/article-images/'",
        "反漂移结构锁转红",
    ),
    Mutation(
        "S12", "T4 既有口径：恢复零图品牌的保底占位",
        "writing/article_generator_service.py",
        "_NO_ASSET_IMAGE_PLACEHOLDERS: tuple = ()",
        "_NO_ASSET_IMAGE_PLACEHOLDERS: tuple = (\"[NEED_IMAGE role=brand_intro purpose=x]\",)",
        "锁4 转红",
    ),
)


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault(
        "TEST_DATABASE_URL",
        "postgresql://geo_admin:test@127.0.0.1:55990/test_geo_agentscope",
    )
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_pytest() -> tuple[int, int, int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", PY_TEST, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    failed = errored = 0
    for count, word in re.findall(r"(\d+) (failed|error|errors)", out):
        if word == "failed":
            failed += int(count)
        else:
            errored += int(count)
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    return proc.returncode, failed, errored, tail


def _run_frontend_lock() -> tuple[int, str]:
    proc = subprocess.run(
        FE_LOCK, cwd=ROOT / "frontend", env=_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        shell=(sys.platform == "win32"),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [line for line in out.strip().splitlines() if line.startswith(("✅", "❌", "🔴"))]
    return proc.returncode, (lines[-1] if lines else out.strip()[-120:])


def _import_smoke() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-c",
         "import services.owned_image_policy, services.safe_markdown_renderer, "
         "writing.article_generator_service"],
        cwd=ROOT, env=_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    err = (proc.stderr or "").strip().splitlines()
    return proc.returncode == 0, (err[-1] if proc.returncode and err else "")


def main() -> int:
    print("=" * 78)
    code, failed, errored, tail = _run_pytest()
    print(f"[baseline pytest] exit={code} failed={failed} errored={errored} | {tail}")
    fe_code, fe_tail = _run_frontend_lock()
    print(f"[baseline 真渲染] exit={fe_code} | {fe_tail}")
    if code != 0 or fe_code != 0:
        print("🔴 基线锁未全绿，变异结果无意义，中止。")
        return 2

    results = []
    for mut in MUTATIONS:
        target = ROOT / mut.path
        backup = target.with_suffix(target.suffix + f".mutbak_{mut.mid}")
        shutil.copy2(target, backup)
        try:
            src = target.read_text(encoding="utf-8")
            if mut.old not in src:
                results.append((mut, "NO_ANCHOR", "变异锚点未命中（代码已漂移）"))
                continue
            target.write_text(src.replace(mut.old, mut.new, 1), encoding="utf-8", newline="\n")

            smoke_ok, smoke_err = _import_smoke()
            if not smoke_ok:
                results.append((mut, "BROKEN", f"import 冒烟失败：{smoke_err[:110]}"))
                continue

            code, failed, errored, tail = _run_pytest()
            detail = f"pytest exit={code} failed={failed} errored={errored}"
            killed = code != 0
            if mut.frontend:
                fe_code, fe_tail = _run_frontend_lock()
                detail += f" | 真渲染 exit={fe_code} {fe_tail[:70]}"
                killed = killed or fe_code != 0
            results.append((mut, "KILLED" if killed else "SURVIVED", detail))
        finally:
            shutil.copy2(backup, target)
            backup.unlink(missing_ok=True)

    print("\n" + "=" * 78)
    killed = survived = broken = no_anchor = 0
    for mut, status, detail in results:
        icon = {"KILLED": "🟢", "SURVIVED": "🔴", "BROKEN": "⚠️", "NO_ANCHOR": "⚠️"}[status]
        print(f"{mut.mid:<4} {icon} {status:<9} {mut.layer}")
        print(f"{'':<5} → {detail}")
        killed += status == "KILLED"
        survived += status == "SURVIVED"
        broken += status == "BROKEN"
        no_anchor += status == "NO_ANCHOR"
    print("-" * 78)
    print(f"总计 {len(results)}：KILLED {killed} · SURVIVED {survived} · "
          f"BROKEN {broken} · NO_ANCHOR {no_anchor}")
    return 0 if survived == broken == no_anchor == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
