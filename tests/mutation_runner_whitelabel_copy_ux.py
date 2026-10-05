"""WO_WHITELABEL_COPY_UX_2026-08-05 · 变异自检(项1 后端 + 项2/3 前端)。

三关缺一即判无效变异:
  1. 锚点唯一命中(命中 0 或 >1 = 变异没打在预期位置);
  2. 落盘后文件真变了;
  3. 至少一个预期用例/锁转红。零转红 = 锁抓不到本 bug。

🔴 文件读写一律走 bytes:Path.read_text/write_text 在 Windows 上会翻换行,
   "读原文再写回"会把整个文件从 LF 翻成 CRLF(2026-08-04/05 两次实栽)。
🔴 变异结果口径:预期红→红 = KILLED;预期红→绿 = SURVIVED(锁失效);
   命中≠1 = SKIP(不许标成 SURVIVED,也不许静默当过)。

跑法: python tests/mutation_runner_whitelabel_copy_ux.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK:emoji/中文输出与子进程 UTF-8 输出都会炸 → 全链 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]

PWL = "services/public_whitelabel.py"
CONTENT_API = "api/content_api.py"
COPYUTILS = "frontend/src/lib/copyUtils.ts"
DIAG_REPORT = "frontend/src/pages/Diagnosis/DiagnosisReport.tsx"
WL_SETTINGS = "frontend/src/pages/Agent/WhitelabelSettings.tsx"

PY_TESTS = "tests/test_whitelabel_copy_ux_2026_08_05.py"
JS_LOCK = "scripts/test-mobile-copy-gesture.mjs"

# (编号, 说明, 文件, 原文锚点, 替换, 跑什么: "py"|"js")
MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    ("W1", "项1 回退:approved 品牌合并基还原为 _PLATFORM_BRAND(NULL 字段继承 OmniRank)",
     PWL,
     "    brand: dict = {k: None for k in _BRAND_FIELDS}\n    brand.update(overrides)",
     "    brand: dict = dict(_PLATFORM_BRAND)\n    brand.update(overrides)",
     "py"),

    ("W2", "项1 连带:content_api 助手名取值回退 None 不安全写法",
     CONTENT_API,
     '        return ((ctx.get("brand") or {}).get("product_name") or "").strip() or _AGENT_DEFAULT_ASSISTANT',
     '        return (ctx.get("brand") or {}).get("product_name", "").strip() or _AGENT_DEFAULT_ASSISTANT',
     "py"),

    ("W3", "项2 回退:禁用 ClipboardItem 同步栈路径(等效于回到旧写法,真机手势栈必丢)",
     COPYUTILS,
     "typeof ClipboardItem !== 'undefined' && window.isSecureContext",
     "false && window.isSecureContext",
     "js"),

    ("W4", "项2 破坏:getText 被打两次(接口重复请求 = 重复扣费风险)",
     COPYUTILS,
     "    const textPromise = getText();\n",
     "    const textPromise = getText(); void getText().catch(() => {});\n",
     "js"),

    ("W5", "项2 回退:DiagnosisReport 不再走 copyAsyncText",
     DIAG_REPORT,
     "        void copyAsyncText(async () => {",
     "        void (async () => {",
     "js"),

    ("W6", "项3 回退:保存成功提示抹掉「客户目前看到的仍是平台品牌」直说",
     WL_SETTINGS,
     "                  : \"设置已保存,但品牌尚未生效(需公司名称 + Logo 齐全)。客户目前看到的仍是平台品牌。\"}",
     "                  : \"设置已保存\"}",
     "js"),
]


def run_py() -> bool:
    """跑项1用例;返回是否全绿。"""
    r = subprocess.run([sys.executable, "-m", "pytest", PY_TESTS, "-q", "--no-header", "-x"],
                       cwd=REPO, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    return r.returncode == 0


def run_js() -> bool:
    r = subprocess.run(["node", JS_LOCK], cwd=REPO / "frontend",
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600, shell=False)
    return r.returncode == 0


def main() -> int:
    results: list[tuple[str, str]] = []

    # 基线必须先绿(基线就红 = 判据比要挡的行为宽,后面全无意义)
    if not run_py():
        print("❌ 基线 pytest 就是红的,变异结果无意义 — 先修基线")
        return 2
    if not run_js():
        print("❌ 基线 js 锁就是红的,变异结果无意义 — 先修基线")
        return 2
    print("✅ 基线双绿(pytest + js 锁)\n")

    for mid, desc, rel, old, new, kind in MUTATIONS:
        path = REPO / rel
        original = path.read_bytes()
        old_b = old.encode("utf-8")
        new_b = new.encode("utf-8")
        hits = original.count(old_b)
        if hits != 1:
            results.append((mid, f"SKIP(锚点命中 {hits} ≠ 1)"))
            print(f"⚠️  {mid} SKIP:锚点命中 {hits} 次 · {desc}")
            continue
        mutated = original.replace(old_b, new_b)
        assert mutated != original
        try:
            path.write_bytes(mutated)
            green = run_py() if kind == "py" else run_js()
        finally:
            path.write_bytes(original)
        if path.read_bytes() != original:
            print(f"🔴 {mid} 还原失败!{rel} 与原文不一致,人工介入")
            return 3
        if green:
            results.append((mid, "SURVIVED"))
            print(f"❌ {mid} SURVIVED(变异后仍绿,锁抓不到):{desc}")
        else:
            results.append((mid, "KILLED"))
            print(f"✅ {mid} KILLED:{desc}")

    killed = sum(1 for _, s in results if s == "KILLED")
    survived = [m for m, s in results if s == "SURVIVED"]
    skipped = [m for m, s in results if s.startswith("SKIP")]
    print(f"\n==== 变异结果:{killed} killed / {len(survived)} survived / {len(skipped)} skip ====")
    if survived:
        print("SURVIVED:", ", ".join(survived))
        return 1
    if skipped:
        print("SKIP(锚点问题,不算通过):", ", ".join(skipped))
        return 1
    print("✅ 全部变异被击杀,锁有判别力")
    return 0


if __name__ == "__main__":
    sys.exit(main())
