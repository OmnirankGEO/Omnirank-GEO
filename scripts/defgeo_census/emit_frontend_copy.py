"""把后端 copy registry 的**指定条目**生成成前端 TS 常量(U-1 / U-10)。

为什么是**生成**而不是手抄
--------------------------
U-1:同一概念全站唯一叫法。前端有一批文案没有服务端往返可依赖
(例如「行业选填」这条 inline 提示,它在用户还没发任何请求时就要上屏),
如果让前端自己写一遍,两边迟早漂。

所以:后端 registry 是唯一 SSOT,这个脚本把需要的条目**机械导出**成
``frontend/src/lib/defensiveGeoCopy.ts``;
``frontend/scripts/verify-defgeo-copy-registry.mjs`` 重新导一次并逐字节比对
—— 漂了就红。它接在 ``npm run lint`` 前置,也可以单跑
``npm run verify:defgeo-copy``;**不接 ``npm run build``**(frontend-builder 是
node:20-alpine,没有 python,那一层也没有后端 registry)。
谁在跑它以 ``frontend/package.json`` 为准,判据
``test_the_verifier_gates_are_exactly_the_wiring_we_claim`` 拿它当唯一事实源。

🔴 分母写在这里(``EXPORTED``),不写在前端。
   前端多写一个 key 会因为"后端导不出来"而红;
   后端改了译文而前端没重新生成,也红。两个方向都堵住。

用法::

    python scripts/defgeo_census/emit_frontend_copy.py            # 写文件
    python scripts/defgeo_census/emit_frontend_copy.py --stdout   # 只打印(判据用)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from services.defensive_geo.copy_registry import (  # noqa: E402
    COPY_REGISTRY_VERSION, census,
)

#: 前端要用的条目。``camelCase key -> (kind, code)``。
#: 顺序即生成顺序 —— 生成结果必须逐字节可重现,否则判据比的是噪声。
EXPORTED: tuple[tuple[str, str, str], ...] = (
    # ── U-5 交付待办清单 ────────────────────────────────────────────
    # ── #63 五卡投影为空时的回落说明 ──
    # [#157 · v11] 题面超限。**带占位符的模板** —— 前端拿到的是模板,
    #   自己 format(ordinal/chars/limit/excerpt),与后端 validate_plan 同一句。
    #   A 的 CTA 要在提交前就禁用并显示它,所以必须进这份名单。
    ("questionTooLong", "reason", "question_too_long"),
    ("fiveCardSummaryUnavailable", "reason", "five_card_summary_unavailable"),
    ("openDeliveryTodo", "action", "open_delivery_todo"),
    ("confirmNextPending", "action", "confirm_next_pending"),
    ("deliveryTodoIntro", "reason", "delivery_todo_intro"),
    ("deliveryTodoAllDone", "reason", "delivery_todo_all_done"),
    # ── U-6 回环自动化 ──────────────────────────────────────────────
    ("confirmUnchangedPrice", "action", "confirm_unchanged_price"),
    ("reviewNewPrice", "action", "review_new_price"),
    ("openApprovedConfirm", "action", "open_approved_confirm"),
    ("previewRebuiltPriceUnchanged", "reason", "preview_rebuilt_price_unchanged"),
    ("previewRebuiltPriceChanged", "reason", "preview_rebuilt_price_changed"),
    ("mediaReplacedSameTier", "reason", "media_replaced_same_tier"),
    ("mediaDowngradedNeedsReconfirm", "reason", "media_downgraded_needs_reconfirm"),
    ("legalRuleHitRepairable", "reason", "legal_rule_hit_repairable"),
    # ── U-9 发给客户 ────────────────────────────────────────────────
    ("openCustomerLinks", "action", "open_customer_links"),
    ("copyCustomerLink", "action", "copy_customer_link"),
    ("reissueCustomerLink", "action", "reissue_customer_link"),
    ("customerLinkExpired", "reason", "customer_link_expired"),
    ("customerLinkRevoked", "reason", "customer_link_revoked"),
    ("customerLinkNotReady", "reason", "customer_link_not_ready"),
    # ── Z-3.1 AI 联网补齐 ───────────────────────────────────────────
    ("aiAutofillFacts", "action", "ai_autofill_facts"),
    ("confirmAiFacts", "action", "confirm_ai_facts"),
    ("editFactsMyself", "action", "edit_facts_myself"),
    ("cancelAiAutofill", "action", "cancel_ai_autofill"),
    ("aiFillIndustry", "action", "ai_fill_industry"),
    ("brandFactsMissing", "reason", "brand_facts_missing"),
    ("aiAutofillNeedsConfirm", "reason", "ai_autofill_needs_confirm"),
    ("industryOptionalDefensive", "reason", "industry_optional_defensive"),
    # ── Z-3.3 广告法一键修复 ────────────────────────────────────────
    ("viewLegalRepairCandidates", "action", "view_legal_repair_candidates"),
    ("applyLegalRepair", "action", "apply_legal_repair"),
    ("editLegalRepair", "action", "edit_legal_repair"),
    # [工单 C-5] 落库成功之后那一句 + 下一步按钮。
    ("retryPublishConfirm", "action", "retry_publish_confirm"),
    ("legalRepairApplied", "reason", "legal_repair_applied"),
    # ── [工单 V5-B · Codex fix-of-fix3 P2-NEW-5 / ③] ─────────────────
    # 🔴 这四条导出来**只给判据用**,不给组件当第二份文案源:
    #    浏览器级判据要回放一个「真实的错误信封」,而它以前回放的是我手写的假文案
    #    (Codex 原话:"浏览器夹具使用了已经修正的假文案,未锁真实信封")。
    #    夹具自己编文案 = 判据在跟自己对话,服务端把话改了它也不会红。
    #    从这里导出之后,夹具用的就是 registry 里那几个字 ——
    #    后端改一个字而没重新生成,``verify-defgeo-copy-registry`` 先红。
    ("pricingDeactivatedAfterPreview", "reason", "pricing_deactivated_after_preview"),
    ("policyUnavailable", "reason", "policy_unavailable"),
    ("newPreviewAction", "action", "new_preview"),
    ("topUpAction", "action", "top_up"),
    ("contactSupportAction", "action", "contact_support"),
    ("insufficientPoints", "reason", "insufficient_points"),
)

HEADER = '''/**
 * 防御型 GEO 前端文案 —— **机械生成,不要手改**。
 *
 * 生成器:``scripts/defgeo_census/emit_frontend_copy.py``
 * SSOT   :``services/defensive_geo/copy_registry.py``(诊断域)
 * 门     :``frontend/scripts/verify-defgeo-copy-registry.mjs``
 *          (``npm run lint`` 前置 / 单跑 ``npm run verify:defgeo-copy``)
 *
 * 为什么前端还要有一份
 * --------------------
 * 大多数文案由服务端随 DTO 下发(``userLabel`` / ``publicExplanation``),
 * 前端只渲染。但有一批提示在**用户还没发任何请求时**就要上屏
 * (例如「所属行业选填」那条 inline 提示)。这些如果让前端自己写,
 * 就会长出第二套口径 —— U-1 明令禁止。
 *
 * 所以这份文件是**导出物**:改文案去改后端 registry,然后重新生成。
 * 手改这里会被上面那道门当场判红(``npm run lint`` 时就会碰到)。
 */

/* eslint-disable */
'''


def render() -> str:
    entries = census()["entries"]
    lines = [HEADER]
    lines.append(f'export const DEFGEO_COPY_REGISTRY_VERSION = {json.dumps(COPY_REGISTRY_VERSION)};\n')
    lines.append("/** 每条文案在后端 registry 里的坐标。门拿它逐条回查。 */")
    lines.append("export const DEFGEO_COPY_SOURCE: Record<string, readonly [string, string]> = {")
    for name, kind, code in EXPORTED:
        lines.append(f"    {name}: [{json.dumps(kind)}, {json.dumps(code)}],")
    lines.append("};\n")
    lines.append("export const DEFGEO_COPY = {")
    for name, kind, code in EXPORTED:
        try:
            text = entries[kind][code]
        except KeyError:
            raise SystemExit(
                f"后端 registry 里没有 {kind}.{code} —— 生成器的分母与 SSOT 对不上"
            )
        lines.append(f"    {name}: {json.dumps(text, ensure_ascii=False)},")
    lines.append("} as const;\n")
    lines.append("export type DefgeoCopyKey = keyof typeof DEFGEO_COPY;")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdout", action="store_true")
    # 🔴 [工单 V5-C · C-4] ``--out`` 给上面那道门用:它要的是**字节**。
    #    不能让门去读 ``--stdout`` —— Windows 上文本模式 stdout 会把 \n 翻成
    #    \r\n,于是一场逐字节比对会变成"永远不一致",而修法看起来像是
    #    "把门关掉"。写文件走的是和正式导出**同一条** newline="" 的路径。
    ap.add_argument("--out", default=None,
                    help="写到指定路径(默认写 frontend/src/lib/defensiveGeoCopy.ts)")
    args = ap.parse_args()
    text = render()
    if args.stdout:
        sys.stdout.write(text)
        return
    out = args.out or os.path.join(
        os.path.dirname(__file__), "..", "..",
        "frontend", "src", "lib", "defensiveGeoCopy.ts",
    )
    # 🔴 newline="" —— Windows 上读改写会把 LF 翻成 CRLF,
    #    然后那道逐字节比对的门会因为换行符判红。
    io.open(os.path.abspath(out), "w", encoding="utf-8", newline="").write(text)
    print(f"wrote {os.path.abspath(out)}  ({len(EXPORTED)} entries)")


if __name__ == "__main__":
    main()
