#!/usr/bin/env python3
"""
whitelabel_leak_scan.py — v3.6 白标泄漏扫描 gate（阶段 0 · T6）

静态扫描源码树中的平台品牌身份 token（OmniRank / 全域上榜 / omnirank.{top,cn,ai} / 小榜），
按 surface 三类样本分类，复用设计文档 §6 / 清单 §C 防火墙白名单排除基础设施噪音：

  - external_only-customer : 客户可见产物（报告/PDF/选词/门户/海报/客户 prompt）应 0 平台名（阶段 1 替换）
  - oem-agent             : 代理后台 UI（含「小榜」）应 0 平台名（阶段 2 自定义后）
  - admin-internal        : admin/支付/OSS/LLM caller/Docker/logger 应【保留】OmniRank（防过度替换）
  - keep_firewall         : §6 防火墙（永不替换 · 不计泄漏）
  - other                 : SSOT / 平台默认常量（如 OMNIRANK_BRAND）等 · 信息项 · 不断言

两模式（设计 §7.5 约束 5 / PLAN §2 验收 gate ⑥）：
  --mode baseline  阶段 0：只统计 + 落基线快照（leak_baseline_phase0.json），**永不因泄漏计数失败**；
                   唯一硬断言 = admin-internal 仍保留 OmniRank（防过度替换 / 误伤防火墙）。
  --mode enforce   阶段 1/2：对 --scope 声明【已替换完成】的范围断言 0 customer/agent 泄漏，
                   失败 exit≠0 阻断（逐层收口：改一层、把该层移出 baseline、enforce 该层）。

阶段 0 只用 baseline；enforce 接口就绪但阶段 0 不实际启用阻断。
本扫描器只做**静态分类**（不渲染产物）；真渲染 E2E（favicon/PDF/HTML 实际输出）见设计 §7.7 staging live-smoke。
classify() 为纯函数，可单测（tests/test_whitelabel_leak_scan.py）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 平台品牌身份 token：CJK 字面 + 大小写不敏感的拉丁名（domains omnirank.top 含 'omnirank' 已覆盖）
TOKEN_RE = re.compile(r"(?i:omnirank)|全域上榜|小榜")

SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".mjs", ".cjs"}
SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv",
    "data", "output", "cache", "logs", ".pytest_cache", ".mypy_cache",
    "coverage", ".idea", ".vscode", "_v36_branding_scan",
}

CLASS_CUSTOMER = "external_only-customer"
CLASS_AGENT = "oem-agent"
CLASS_ADMIN = "admin-internal"
CLASS_KEEP = "keep_firewall"
CLASS_OTHER = "other"

# customer-surface path 前缀（客户可见产物 · 清单 §A）
_CUSTOMER_PATHS = (
    "services/report_html_renderer",
    "services/report_writer_v2.py",
    "services/report_v3_pdf_footer.py",
    "agents/report_enhancement_agent.py",
    "writing/templates/",
    "frontend/src/pages/Public/",
    "frontend/src/pages/Selection/",
    "frontend/src/pages/MaterialConfirm/",
    "frontend/src/pages/Portal/",
    "frontend/src/pages/Intake/",
    "frontend/src/components/c_end/",
)
# agent-surface path（代理后台 / agent-facing prompt · 清单 §B）
_AGENT_PATHS = (
    "frontend/src/pages/",
    "frontend/src/components/layout/",
    "api/xiaobang_api.py",
    "api/content_api.py",
    "api/publish_api.py",
    # [WO_273] 原有插件后端一项;文件随插件后端删除,前缀同步去掉。
    # [开源 E3 · B2 · 2026-09-28] 原有社媒 agent 一项;文件随社媒删除,同步去掉。
    "agents/geo_agent.py",
    "agents/xiaobang_presets.py",
)


def _js_comment_before(line: str, start: int) -> bool:
    """token 前是否出现 JS 行注释 //（排除 URL scheme 的 ://）。"""
    for m in re.finditer(r"//", line):
        if m.start() >= start:
            break
        if m.start() == 0 or line[m.start() - 1] != ":":
            return True
    return False


def classify(relpath: str, line: str, token: str, start: int) -> str:
    """纯分类逻辑（可单测）。first-match-wins，顺序：防火墙 → admin → customer → agent → other。"""
    p = relpath.replace("\\", "/")
    pl = p.lower()
    tok = token.lower()
    low_line = line.lower()

    # ---------- KEEP / §6 防火墙（不计泄漏） ----------
    # 归档 / 文档 / legacy（不渲染）
    if "/_archive/" in p or p.endswith("_LEGACY.md") or pl.startswith("docs/"):
        return CLASS_KEEP
    # 测试 / 迁移 / 种子
    if pl.startswith("tests/") or "/migrations/" in pl:
        return CLASS_KEEP
    # 部署 / 基础设施配置
    if (pl.endswith(".sh") or pl.endswith(".yml") or pl.endswith(".yaml")
            or "dockerfile" in pl or "docker-compose" in pl or "nginx" in pl):
        return CLASS_KEEP
    # 代码符号（改名即坏：插件全局对象 / DI 类型）
    if "omnirankextension" in low_line or "omnirankdeps" in low_line:
        return CLASS_KEEP
    # 基础设施/代码符号名(token 后紧接 '-' 或 '_'）：
    #   '-' → Docker/OSS/lock(omnirank-blue/green/ai/db/kyc/...);
    #   '_' → localStorage/鉴权/缓存键(omnirank_token/omnirank_publish_cart/omnirank_c_end_mode...)
    #         = §6「代码符号 改名即坏」同类,非品牌展示。
    nxt = line[start + len(token): start + len(token) + 1]
    if tok == "omnirank" and nxt in ("-", "_"):
        return CLASS_KEEP
    if "/tmp/omnirank" in low_line:
        return CLASS_KEEP
    # 支付主体 / 短信签名 / 订阅法律主体（法规绑定 · 决策 G KEEP）
    if ("wechat_pay" in pl or "xunhupay" in pl or "/payment/" in pl
            or pl.endswith("sms_service.py")
            # [开源 E3 · B4 · 2026-09-28] 订阅支付模块整文件删除,原「订阅法律主体」那条按文件名的放行随之去掉
            or "subscriptionsigningpage" in pl):
        return CLASS_KEEP
    # LLM caller header / 爬虫 UA（行级标识）
    if any(h in line for h in ("HTTP-Referer", "X-Title", "User-Agent", "Referer")):
        return CLASS_KEEP
    # 代码注释（# for py / // for js，排除 URL 的 ://）
    hsh = line.find("#")
    if pl.endswith(".py") and 0 <= hsh < start:
        return CLASS_KEEP
    if pl.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")) and _js_comment_before(line, start):
        return CLASS_KEEP

    # ---------- admin-internal（必须保留 OmniRank） ----------
    if ("/pages/admin/" in pl or pl.startswith("api/admin")
            or pl.endswith("admin_api.py") or "/admin/" in pl and pl.endswith((".tsx", ".ts"))):
        return CLASS_ADMIN

    # ---------- customer（external_only 客户可见） ----------
    if any(cp in p for cp in _CUSTOMER_PATHS) or "generate_pdf_themes" in pl:
        return CLASS_CUSTOMER

    # ---------- agent（oem 代理后台） ----------
    if any(ap in p for ap in _AGENT_PATHS):
        return CLASS_AGENT

    # ---------- other（SSOT / 平台默认常量 · 信息项不断言） ----------
    return CLASS_OTHER


def _block_comment_mask(line: str, in_block: bool) -> tuple[list, bool]:
    """跟踪 JS/TS 块注释 /* */（含跨行、JSX {/* */}）。

    返回 (mask, in_block_after)：mask[i]=True 表示该字符在块注释内（不渲染 → KEEP）。
    Python 无 /* */，调用方仅对 JS/TS 文件启用。
    """
    mask = [False] * len(line)
    j = 0
    n = len(line)
    while j < n:
        if not in_block and line[j:j + 2] == "/*":
            in_block = True
            mask[j] = True
            if j + 1 < n:
                mask[j + 1] = True
            j += 2
            continue
        if in_block:
            mask[j] = True
            if line[j:j + 2] == "*/":
                if j + 1 < n:
                    mask[j + 1] = True
                in_block = False
                j += 2
                continue
        j += 1
    return mask, in_block


def scan_text(relpath: str, text: str) -> list[dict]:
    """扫描单文件文本，返回每处命中（含分类）。块注释内的命中归 KEEP（不渲染）。"""
    out: list[dict] = []
    pl = relpath.lower()
    is_js = pl.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))
    in_block = False
    for i, line in enumerate(text.splitlines(), 1):
        if is_js:
            mask, in_block = _block_comment_mask(line, in_block)
        else:
            mask = None
        for m in TOKEN_RE.finditer(line):
            tok = m.group(0)
            start = m.start()
            if mask is not None and start < len(mask) and mask[start]:
                cls = CLASS_KEEP  # 块注释 /* */ 内 · 不渲染（§6 KEEP）
            else:
                cls = classify(relpath, line, tok, start)
            snippet = line.strip()
            if len(snippet) > 200:
                snippet = snippet[:200] + "…"
            out.append({
                "file": relpath, "line": i, "col": start + 1,
                "token": tok, "class": cls, "snippet": snippet,
            })
    return out


def iter_source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SCAN_EXTS:
                yield Path(dirpath) / fn


def scan_tree(root: Path) -> list[dict]:
    findings: list[dict] = []
    for fp in iter_source_files(root):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        low = text.lower()
        if "omnirank" not in low and "全域上榜" not in text and "小榜" not in text:
            continue
        rel = str(fp.relative_to(root)).replace("\\", "/")
        findings.extend(scan_text(rel, text))
    return findings


def summarize(findings: list[dict]):
    summary: dict[str, int] = {}
    by_class: dict[str, list[dict]] = {}
    for f in findings:
        c = f["class"]
        summary[c] = summary.get(c, 0) + 1
        by_class.setdefault(c, []).append(f)
    summary["total"] = len(findings)
    return summary, by_class


def admin_internal_keeps_omnirank(summary: dict) -> bool:
    """阶段 0 唯一硬断言：admin-internal 类仍保留 OmniRank（>0 = 未被过度替换）。"""
    return summary.get(CLASS_ADMIN, 0) > 0


def _git_head(root: Path) -> str:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="v3.6 白标泄漏扫描 gate（baseline/enforce）")
    ap.add_argument("--mode", choices=["baseline", "enforce"], default="baseline")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--scope", nargs="*", default=None,
                    help="enforce 模式：声明【已替换完成】的文件 glob（相对 root），断言 0 customer/agent 泄漏")
    ap.add_argument("--out", default=None,
                    help="baseline 快照输出路径（默认 docs/AI-CONTEXT/_v36_branding_scan/leak_baseline_phase0.json）")
    args = ap.parse_args(argv)

    # Windows 控制台默认 GBK，无法输出 emoji/CJK → 强制 UTF-8（不影响快照 JSON 已是 utf-8）
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    root = Path(args.root).resolve()
    findings = scan_tree(root)
    summary, by_class = summarize(findings)
    admin_ok = admin_internal_keeps_omnirank(summary)

    print("=== v3.6 白标泄漏扫描 ===")
    print(f"mode={args.mode} root={root}")
    for c in (CLASS_CUSTOMER, CLASS_AGENT, CLASS_ADMIN, CLASS_KEEP, CLASS_OTHER):
        print(f"  {c:24s}: {summary.get(c, 0)}")
    print(f"  {'total':24s}: {summary.get('total', 0)}")

    if args.mode == "baseline":
        out = Path(args.out) if args.out else (
            root / "docs/AI-CONTEXT/_v36_branding_scan/leak_baseline_phase0.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "phase": "phase0_baseline",
            "baseline_commit": _git_head(root),
            "note": ("阶段 0 未替换 UI/Prompt，external_only-customer / oem-agent 必有已知漏点"
                     "=阶段 1/2 待修工单；baseline 不因泄漏计数失败。唯一硬断言=admin-internal 保留 OmniRank。"),
            "summary": summary,
            "by_class": by_class,
        }
        out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[baseline] 基线快照已写 → {out}")
        if not admin_ok:
            print("❌ 硬断言失败：admin-internal 类 0 处 OmniRank → 疑似过度替换 / 误伤防火墙。", file=sys.stderr)
            return 2
        print(f"✅ 硬断言通过：admin-internal 保留 OmniRank（{summary.get(CLASS_ADMIN, 0)} 处）。")
        print("[baseline] 永不因 customer/agent 泄漏计数失败（=阶段 1/2 待修工单基线）。")
        return 0

    # enforce（阶段 1/2）
    if not args.scope:
        print("❌ enforce 模式必须用 --scope 声明【已替换完成】的文件范围（glob，相对 root）。", file=sys.stderr)
        return 2
    import fnmatch

    def in_scope(rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pat) for pat in args.scope)

    if not admin_ok:
        print("❌ 硬断言失败：admin-internal 类 0 处 OmniRank → 疑似过度替换。", file=sys.stderr)
        return 2
    leaks = [f for f in findings
             if f["class"] in (CLASS_CUSTOMER, CLASS_AGENT) and in_scope(f["file"])]
    if leaks:
        print(f"❌ enforce 失败：声明已替换范围内仍有 {len(leaks)} 处平台名泄漏：", file=sys.stderr)
        for f in leaks[:50]:
            print(f"   {f['file']}:{f['line']} [{f['class']}] {f['token']} · {f['snippet']}", file=sys.stderr)
        return 1
    print(f"✅ enforce 通过：scope={args.scope} 内 0 处 customer/agent 泄漏。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
