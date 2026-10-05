#!/usr/bin/env python
"""变异测试 —— 证明 ``test_media_provider_routing.py`` 那组锁**咬得住**。

存在的理由:一组恒绿的断言和没有断言是一回事。这个 runner 逐个把真实源码改坏,
要求测试**当场变红**;再跑一遍未改动的原码,要求**全绿** —— 后者是反向对照,
证明这些锁不是恒红。

> 仓内教训(2026-07-31 `preflight_selftest` 变异③):最初断言打在一个**无条件打印的
> 章节标题**上,好包也命中 = 什么都没证明。**每个"必须命中"都要配一个成对的
> "必须不命中"。**单向断言证明不了判别力。

用法::

    python tests/mutation_media_provider_routing.py

退出码 0 = 全部变异都被抓住 且 原码全绿。
"""
from __future__ import annotations

import os
import subprocess
import sys

# Windows 控制台默认 GBK,打不出 ✅/🔴 会直接 UnicodeEncodeError 把 runner 打断 ——
# 那会表现成"变异测试跑不起来",而不是"变异没被抓住",容易误判。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FILE = os.path.join("tests", "test_media_provider_routing.py")

EQUIV = os.path.join(ROOT, "services", "media_provider_equivalence.py")
ROUTING = os.path.join(ROOT, "services", "kuaiyibo", "routing.py")
STATUS = os.path.join(ROOT, "services", "kuaiyibo", "status_sync.py")
API = os.path.join(ROOT, "api", "meijiehezi_api.py")

NL = "\n"

#: (标签, 文件, 原串, 改成, 预期被哪条测试抓到)
#: 每一条都对应一个**真实亏钱/亏交付**的失效模式,不是随机改字符。
MUTATIONS = [
    (
        "M1 一律走 kyb(A2 要抓的就是这个)",
        EQUIV,
        'preferred = "kyb" if saving > 0 else "mhz"',
        'preferred = "kyb"',
        "test_a2_mhz_cheaper_prefers_mhz",
    ),
    (
        "M2 高价未佐证也自动进表(踩 §2 红线)",
        EQUIV,
        "    if fans_corroborated(row.get(\"mhz_fans\"), row.get(\"kyb_fans\")):\n"
        "        return CONFIDENCE_FANS_OK, preferred\n    return None, preferred",
        "    return CONFIDENCE_FANS_OK, preferred",
        "test_high_value_without_fans_proof_is_human_only",
    ),
    (
        "M3 分档门槛按比值而非绝对金额(§7.4)",
        EQUIV,
        "if saving <= HIGH_VALUE_THRESHOLD_YUAN:",
        "if float(row['kyb_price']) / max(float(row['mhz_price']), 1e-9) > 0.5:",
        "test_threshold_is_absolute_amount_not_ratio",
    ),
    (
        "M4 匹配读 mhz.source_domain(§6 回填会回头改风险敞口)",
        EQUIV,
        "           {_MHZ_DOMAIN_EXPR} AS dom\n      FROM {table_name}\n     WHERE provider = 'mhz'",
        "           lower(source_domain) AS dom\n      FROM {table_name}\n     WHERE provider = 'mhz'",
        "test_backfill_cannot_change_matching",
    ),
    (
        "M5 一对多不取最低价",
        EQUIV,
        " ORDER BY m.id, k.price ASC, k.id ASC",
        " ORDER BY m.id, k.id ASC",
        "test_match_sql_requires_both_active_and_takes_cheapest",
    ),
    (
        "M6 查询层丢掉「必须更便宜」条件",
        EQUIV,
        "               AND kyb_price < mhz_price\n",
        "",
        "test_a2_lookup_sql_excludes_non_cheaper",
    ),
    (
        "M7 改道兜底闸失效(脏数据就能改去更贵的一家)",
        ROUTING,
        'if new_cost >= float(target["mhz_price"]):',
        "if False:",
        "test_never_route_to_more_expensive",
    ),
    (
        "M8 就地改原 item(调用方去重/展示串味)",
        ROUTING,
        "            copy = dict(item)",
        "            copy = item",
        "test_original_items_never_mutated",
    ),
    (
        "M9 自记账沿用 mhz 价(给快易播充值判断被带偏)",
        ROUTING,
        '            copy["cost_yuan"] = new_cost',
        "            pass",
        "test_ledger_cost_follows_routing",
    ),
    (
        "M10 总开关失效(A8 回滚回不去)",
        ROUTING,
        "        if not is_routing_enabled():\n            return items, {}",
        "        pass",
        "test_disabled_switch_is_bit_identical",
    ),
    (
        "M11 择优出错时硬失败(挡住用户发稿)",
        ROUTING,
        '        logger.warning("[routing] 择优查表失败,按原渠道投递: %s", exc)\n        return items, {}',
        "        raise",
        "test_fail_soft_on_lookup_error",
    ),
    (
        "M12 🔴 状态回流认不出改道单(稿发了却被退款)",
        STATUS,
        "    routed = item.get(\"routed_media_id\")\n    return routed if routed is not None else item.get(\"media_id\")",
        "    return item.get(\"media_id\")",
        "test_routed_item_detected_as_kyb",
    ),
    (
        "M13 扫描 SQL 漏取 routed_media_id",
        STATUS,
        "            SELECT id, user_id, media_id, routed_media_id, media_name,\n"
        "                   mhz_order_id, cost_points, status\n"
        "              FROM mhz_publish_order_items\n"
        "             WHERE status = ANY(%s)\n"
        "               AND COALESCE(mhz_order_id, '') <> ''",
        "            SELECT id, user_id, media_id, media_name,\n"
        "                   mhz_order_id, cost_points, status\n"
        "              FROM mhz_publish_order_items\n"
        "             WHERE status = ANY(%s)\n"
        "               AND COALESCE(mhz_order_id, '') <> ''",
        "test_all_scanners_select_routed_media_id",
    ),
    (
        "M21 🔴 域名口径退回不对称(kyb 侧改读注册域 → 249 条错配回来 + 静默少配)",
        EQUIV,
        "_KYB_DOMAIN_EXPR = _HOST_FROM_CASE_LINK",
        '_KYB_DOMAIN_EXPR = r"lower(regexp_replace(source_domain, \'^www\\.\', \'\'))"',
        "test_domain_expr_is_symmetric",
    ),
    (
        "M22 两边都退到注册域(toutiao.com 下 18,745 个账号 → 主动放大撞名)",
        EQUIV,
        "_HOST_FROM_CASE_LINK = (\n"
        "    r\"lower(regexp_replace(\"\n"
        "    r\"substring(case_link from '^https?://([^/:?#]+)'), '^www\\.', ''))\"\n"
        ")",
        "_HOST_FROM_CASE_LINK = (\n"
        "    r\"lower(regexp_replace(\"\n"
        "    r\"substring(case_link from '^https?://(?:[^/:?#]*\\.)?([^./:?#]+\\.[^./:?#]+)'), '^www\\.', ''))\"\n"
        ")",
        "test_registrable_domain_symmetry_is_not_used",
    ),
    (
        "M15 🔴 路由对账挪出 finally(只有成功分支撤标记 → 异常路径留脏标记)",
        API,
        "            finally:" + NL + "                # 🔴 路由对账",
        "            if True:" + NL + "                # 🔴 路由对账",
        "test_api_reconciles_routing_on_every_exit_path",
    ),
    (
        "M16 _routed_submitted 挪进 try(finally 里 NameError 盖掉真异常)",
        API,
        "                from services.article_publish_dispatch import dispatch_article_to_provider"
        + NL + NL + "                _dispatch_source = ",
        "                _routed_submitted: set[int] = set()" + NL
        + "                from services.article_publish_dispatch import dispatch_article_to_provider"
        + NL + NL + "                _dispatch_source = ",
        "test_routed_submitted_initialized_outside_try",
    ),
    (
        "M17 撤销丢掉「只碰有标记的行」守卫(一次误调用刷全表)",
        ROUTING,
        " WHERE id = ANY(%s) AND routed_media_id IS NOT NULL",
        " WHERE id = ANY(%s)",
        "test_clear_routing_only_touches_marked_rows",
    ),
    (
        "M18 撤销对空输入不早退(退化成无 WHERE 全表 UPDATE)",
        ROUTING,
        "    if not ids:" + NL + "        return 0",
        "    if False:" + NL + "        return 0",
        "test_clear_routing_ignores_empty_input",
    ),
    (
        "M19 🔴 拒稿退款取我方外采价而非用户实付(用户少拿钱 · 静默)",
        STATUS,
        '    cost = int(item.get("cost_points") or 0)',
        '    cost = int(float(item.get("routed_cost_yuan") or item.get("cost_points") or 0))',
        "test_routed_refund_amount_is_user_paid_not_provider_cost",
    ),
    (
        "M20 🔴 退款幂等键改成 order 维度(仓内三套键里错的那两套之一)",
        STATUS,
        'refund_key=f"item:{item[\'id\']}",   # 仓内唯一正确的退款幂等键',
        'refund_key=f"order:{item.get(\'mhz_order_id\')}",',
        "test_routed_rejected_item_refunds_with_item_key",
    ),
    (
        "M14 λ 不参与计算(将来调 λ 悄无声息没反应)",
        EQUIV,
        "    return kyb + (1.0 - lam) * (float(mhz_cost) - kyb)",
        "    return float(mhz_cost)",
        "test_lambda_actually_participates",
    ),
]


def run_tests() -> tuple[int, str]:
    # 🔴 必须显式 utf-8 解码:Windows 默认 GBK,pytest 输出里的中文断言消息会
    #   UnicodeDecodeError 把 stdout 变成 None —— 那时"抓没抓住"这个判据本身就没了。
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    print("=" * 78)
    print("变异测试 · 双供应商择优路由锁")
    print("=" * 78)

    # ---- 反向对照(先跑):原码必须全绿,否则后面"变红"什么都证明不了 ----
    code, out = run_tests()
    if code != 0:
        print("🔴 原码就是红的 —— 变异测试无意义,先修测试本身\n")
        print(out[-4000:])
        return 1
    baseline_line = [ln for ln in out.splitlines() if "passed" in ln]
    print(f"✅ 反向对照:未改动原码全绿  {baseline_line[-1] if baseline_line else ''}\n")

    failures: list[str] = []
    for label, path, old, new, expect_test in MUTATIONS:
        # 🔴 newline="" 两处都不能少:Windows 上文本模式写会把 \n 翻成 \r\n,
        #   跑一次变异就把被碰过的源文件整份 CRLF 化 —— 表现是"我只改了 4 行,
        #   git diff 却说 798 行",diff 直接没法审。实测踩过一次。
        with open(path, "r", encoding="utf-8", newline="") as f:
            original = f.read()
        if old not in original:
            failures.append(f"{label}:变异锚点在源码里找不到(锚点写错 = 这条变异什么都没测)")
            print(f"🔴 {label}\n   锚点未命中,跳过")
            continue
        occurrences = original.count(old)
        if occurrences != 1:
            failures.append(f"{label}:锚点命中 {occurrences} 处,不唯一")
            print(f"🔴 {label}\n   锚点命中 {occurrences} 处,不唯一")
            continue
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(original.replace(old, new, 1))
            code, out = run_tests()
            if code == 0:
                failures.append(f"{label}:改坏了测试却still绿 —— 这条锁咬不住")
                print(f"🔴 {label}\n   改坏后仍全绿 = 锁无效")
            elif expect_test not in out:
                failures.append(
                    f"{label}:变红了,但不是预期的 {expect_test} 抓到的(可能是连带塌方)")
                print(f"⚠️  {label}\n   变红但预期测试 {expect_test} 未出现在失败列表")
            else:
                print(f"✅ {label}\n   → 被 {expect_test} 当场抓住")
        finally:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(original)

    # ---- 收尾再跑一次,确认所有源码都已复原 ----
    code, _ = run_tests()
    if code != 0:
        print("\n🔴 复原失败:跑完变异后原码不再全绿,请 git checkout 相关文件")
        return 1

    print("\n" + "=" * 78)
    if failures:
        print(f"结果:{len(MUTATIONS) - len(failures)}/{len(MUTATIONS)} 通过 · 以下未通过")
        for f in failures:
            print("  🔴 " + f)
        return 1
    print(f"结果:{len(MUTATIONS)}/{len(MUTATIONS)} 全部变异被抓住 · 原码复原后仍全绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
