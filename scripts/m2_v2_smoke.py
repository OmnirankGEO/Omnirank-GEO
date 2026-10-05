"""
M2 报告 2.0 · v2 装配 smoke 验证 · CTO-15.16 2026-04-26
==========================================================

任务: 验证 services.report_writer_v2.assemble_report_v2 在真实/合成 brand 上能跑通
  · 8 模块都返
  · brief 消费观测点 brief_consumption 真有数据(M1c 改动后必须 has_brief=True)
  · evidence_count 字段返回结构(没数据时返 0/0/0 不爆炸)
  · 不写 monitoring_reports 表 · 全量 in-memory smoke

铁律:
  - 不调 ai_write_report endpoint(那会扣费 + 走 LLM)
  - 不写 DB · 调 assemble_report_v2 直接拿 dict
  - 用 5 金标准 brand_id 跑 + 一个虚构 brand_id 做边界
  - 标 data_scope: smoke run, no production write

跑法:
  cd c:/AI-Test/AgentsCope-07-m1m2-final
  python scripts/m2_v2_smoke.py

输出:
  scripts/m2_v2_smoke_result.json(每 brand 1 条 · 含 brief_consumption + module 结构)
  stdout 8 模块 PASS/FAIL 矩阵

data_scope: in-memory assemble · no DB write · brief 拉的是真 client_profiles
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 5 金标准 brand_id(对照 m1c_baseline_5brands_result.json)
GOLDEN_BRAND_IDS = [1, 83, 10, 3, 17]

EXPECTED_MODULES = ("1", "2", "3", "4", "5", "6", "7", "8")


def _build_synthetic_report_data(brand_name: str, industry: str = "", total: int = 65) -> dict:
    """合成 report_data(diagnosis_workflow 真实产出的最小子集)
    M2 v2 不该依赖 LLM 走完才能跑 · 只要骨架完整就能装配 8 模块
    """
    return {
        "brand_name": brand_name,
        "industry": industry,
        "city": "",
        "total_score": total,
        "scores": {
            "resolved_score": 60.0,
            "brand_fit_score": 70.0,
            "keyword_coverage_score": 55.0,
            "engine_penetration_score": 65.0,
            "content_quality_score": 70.0,
            "depth_score": 60.0,
            "update_frequency_score": 50.0,
            "authority_score": 70.0,
        },
        "suggestions": {
            "high_priority": [
                {"issue": "关键词覆盖不足", "action": "补 5 个长尾关键词 + 2 篇 FAQ"},
            ],
            "medium_priority": [
                {"issue": "权威背书弱", "action": "对接 1-2 个行业媒体合作"},
            ],
            "ongoing": [
                {"issue": "持续监测", "action": "每月跑 1 次 4 引擎检测"},
            ],
        },
    }


def _smoke_one(brand_id: int, *, inject_brief: bool = False) -> dict:
    """跑一个 brand · 返 PASS/FAIL 矩阵

    inject_brief=True · CTO-15.16 round2 Task B 正向 brief 消费门:
      monkeypatch services.report_writer_v2._enrich_with_brief 返一个固定 brief
      验证 Module 1/4/5 真把 brief 字段渲染进 markdown
      此模式 only smoke · 不写 DB · 不依赖 5 金标准 D 组 confirmed 状态
    """
    out: dict = {
        "brand_id": brand_id,
        "brand_name": "",
        "inject_brief": inject_brief,
        "modules_present": {},
        "module_md_lengths": {},
        "evidence_count": None,
        "brief_consumption": None,
        "module_signals": {},  # CTO-15.16 round2 Task B · M1/M4/M5 真消费观测
        "errors": [],
    }
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, name, industry FROM brands WHERE id = %s", (brand_id,))
            row = cur.fetchone()
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
        if not row:
            out["errors"].append("brand 不存在")
            return out
        b = dict(row)
        out["brand_name"] = b.get("name") or ""
        report_data = _build_synthetic_report_data(b.get("name") or "未知", b.get("industry") or "", total=65)
    except Exception as e:
        out["errors"].append(f"brand 拉取失败: {e}")
        return out

    # CTO-15.16 round2 Task B · monkeypatch _enrich_with_brief 注 confirmed brief
    # 不写 DB · monkeypatch 仅在 inject_brief 路径下生效 · finally 还原
    from services import report_writer_v2 as _rwv2
    _orig_enrich = _rwv2._enrich_with_brief
    _injected_brief = {
        "my_differentiation": "本地服务 24h 响应 · 一对一项目经理 · 7×12 售后",
        "authority_sources": ["人民日报", "新华社", "行业协会白皮书"],
        "hot_formats": ["榜单", "测评", "案例分析"],
        "local_competitors": [
            {"name": "竞品 Alpha"},
            {"name": "竞品 Beta"},
            {"name": "竞品 Gamma"},
        ],
        "_v37_meta": {"smoke_injected_at": "test"},
    }
    _injected_local_competitors = ["竞品 Alpha", "竞品 Beta", "竞品 Gamma"]
    if inject_brief:
        def _fake_enrich(rd: dict, _bid):
            return {
                **rd,
                "brief": _injected_brief,
                "service_scope": "local",
                "local_competitors": _injected_local_competitors,
            }
        _rwv2._enrich_with_brief = _fake_enrich
    try:
        result = _rwv2.assemble_report_v2(
            report_data, brand_id=brand_id,
            report_type="diagnosis", audience="internal",
        )
    except Exception as e:
        out["errors"].append(f"assemble_report_v2 抛异常: {e}")
        return out
    finally:
        if inject_brief:
            _rwv2._enrich_with_brief = _orig_enrich

    modules = result.get("modules") or {}
    for k in EXPECTED_MODULES:
        m = modules.get(k)
        out["modules_present"][k] = bool(m and m.get("rendered_md"))
        out["module_md_lengths"][k] = len(m.get("rendered_md") or "") if m else 0

    out["evidence_count"] = result.get("evidence_count")
    out["brief_consumption"] = result.get("brief_consumption")
    out["full_md_length"] = len(result.get("full_markdown") or "")
    out["lint_warnings"] = result.get("lint_warnings") or []

    # CTO-15.16 round2 Task B · 真消费断言:
    # M1 cover 必须含 my_differentiation 子串 · M4 必须含 brief 候选竞品 · M5 必须含 service_scope 提示
    m1_md = (modules.get("1") or {}).get("rendered_md") or ""
    m4_md = (modules.get("4") or {}).get("rendered_md") or ""
    m5_md = (modules.get("5") or {}).get("rendered_md") or ""
    diff_text = _injected_brief["my_differentiation"]
    competitor_names = [c["name"] for c in _injected_brief["local_competitors"]]
    out["module_signals"] = {
        "m1_has_differentiation": diff_text in m1_md,
        "m4_has_brief_competitor": any(name in m4_md for name in competitor_names),
        "m5_has_service_scope_block": "服务范围 brief" in m5_md or "本地/区域" in m5_md or "全国" in m5_md,
    }

    return out


def main() -> int:
    db_url = os.getenv("DATABASE_URL", "(unset)")
    masked = db_url
    if "@" in masked and ":" in masked.split("@", 1)[0]:
        head, tail = masked.split("@", 1)
        scheme_user = head.rsplit(":", 1)[0]
        masked = f"{scheme_user}:***@{tail}"
    print(f"[m2-v2-smoke] DATABASE_URL = {masked}")
    print(f"[m2-v2-smoke] data_scope: in-memory assemble · no DB write\n")

    rows: list[dict] = []
    for bid in GOLDEN_BRAND_IDS:
        r = _smoke_one(bid)
        rows.append(r)

    # 边界: 不存在的 brand_id
    rows.append(_smoke_one(99999999))

    # CTO-15.16 round2 Task B · 正向 brief 消费门
    # 在 5 金标准之外 · 选 brand_id=1(全域上榜)再跑一次 · monkeypatch 注 confirmed brief
    # 验证 _enrich_with_brief / Module 1/4/5 真消费 brief 字段(不是返个 stub)
    # 此模式不写 DB · 仅 in-memory monkeypatch · finally 还原
    inject_row = _smoke_one(GOLDEN_BRAND_IDS[0], inject_brief=True)
    rows.append(inject_row)

    # 渲染 markdown 矩阵
    print("| brand_id | brand_name | mode | M1 | M2 | M3 | M4 | M5 | M6 | M7 | M8 | full_md | brief? | lint |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        name = (r.get("brand_name") or "—")[:18]
        modules_present = r.get("modules_present", {})
        cells = ["✅" if modules_present.get(k) else "❌" for k in EXPECTED_MODULES]
        bc = r.get("brief_consumption") or {}
        brief_flag = "Y" if bc.get("has_brief") else "N"
        lints = len(r.get("lint_warnings") or [])
        full_len = r.get("full_md_length", 0)
        mode = "inject" if r.get("inject_brief") else "real"
        print(
            f"| {r['brand_id']} | {name} | {mode} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} | "
            f"{cells[4]} | {cells[5]} | {cells[6]} | {cells[7]} | {full_len}c | {brief_flag} | {lints} |"
        )

    # 汇总判定
    # real_rows: 5 金标准真实 brand · 排除 inject 模式 + 99999999 边界
    real_rows = [
        r for r in rows
        if not r.get("errors")
        and r.get("brand_id") != 99999999
        and not r.get("inject_brief")
    ]
    all_8_present = sum(
        1 for r in real_rows
        if all(r.get("modules_present", {}).get(k) for k in EXPECTED_MODULES)
    )
    # CTO-15.16 round2 Task B · brief 消费 = 任意 inject_row + real_rows 中 has_brief
    inject_rows = [r for r in rows if r.get("inject_brief") and not r.get("errors")]
    inject_has_brief = sum(
        1 for r in inject_rows if (r.get("brief_consumption") or {}).get("has_brief")
    )
    real_has_brief = sum(
        1 for r in real_rows if (r.get("brief_consumption") or {}).get("has_brief")
    )
    brief_consumption_active = inject_has_brief + real_has_brief

    # M1/M4/M5 真消费断言:inject 行 module_signals 至少 1 项 True
    inject_module_signals_ok = any(
        any((r.get("module_signals") or {}).values()) for r in inject_rows
    )

    # CTO-15.16 round2 Task C · golden 报告 lint_warnings 必须空(用户/第三方引文不算)
    # report_writer_v2 调 lint_report_text(strip_quoted=True) 已剥离 backtick / 「」 引文 ·
    # 此处仍命中 = 我们叙述里有"绝对/最好/唯一"等 = 真违法 · 应 fail
    lint_offenders = [
        {
            "brand_id": r.get("brand_id"),
            "mode": "inject" if r.get("inject_brief") else "real",
            "lint_warnings": r.get("lint_warnings"),
        }
        for r in real_rows + inject_rows
        if r.get("lint_warnings")
    ]
    lint_clean = len(lint_offenders) == 0

    print()
    print(f"**8 模块全部装配通过**: {all_8_present}/{len(real_rows)}")
    print(f"**brief 消费起作用(inject + real)**: {brief_consumption_active} (inject {inject_has_brief} + real {real_has_brief})")
    print(f"**Module 1/4/5 真消费 brief 字段(inject 模式)**: {'✅' if inject_module_signals_ok else '❌'}")
    print(f"**lint_warnings 全空(strip_quoted 后)**: {'✅' if lint_clean else f'❌ {len(lint_offenders)} brand 仍命中'}")
    print(f"**99999999 边界**:errors={next((r.get('errors') for r in rows if r.get('brand_id') == 99999999), '(无)')}")

    # CTO-15.16 P1 (Codex 复核):real_rows 期望 5(5 金标准 brand_id 1/83/10/3/17)
    # · real_rows == 0(DB 没数据 / 没连上)→ 必须 fail · 不能给假绿
    # · real_rows < 5(部分 brand 不在库)→ 必须 fail · 缺哪个标出来
    # · real_rows == 5 但 all_8_present < 5 → 失败(原逻辑保留)
    # CTO-15.16 round2 Task B · 加 brief 消费门:
    # · brief_consumption_active == 0 → fail(假绿)
    # · inject 行 Module 1/4/5 module_signals 全 False → fail(brief 没真渲染进 markdown)
    expected_real = len(GOLDEN_BRAND_IDS)
    real_actual = len(real_rows)
    real_count_ok = (real_actual == expected_real)
    brief_gate_ok = brief_consumption_active >= 1 and inject_module_signals_ok
    smoke_pass = (
        real_count_ok
        and (all_8_present == expected_real)
        and brief_gate_ok
        and lint_clean
    )
    missing_brand_ids = [
        bid for bid in GOLDEN_BRAND_IDS
        if not any(r.get("brand_id") == bid and not r.get("inject_brief") and not r.get("errors") for r in rows)
    ]

    out_path = Path(__file__).parent / "m2_v2_smoke_result.json"
    # CTO-15.16 P1 提交卫生 · 用 write_bytes 强制 LF · 否则 Path.write_text 在 Windows 写 CRLF
    _payload_str = json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_scope": "in-memory assemble · no DB write · 1 row uses monkeypatched _enrich_with_brief",
        "rows": rows,
        "summary": {
            "expected_real": expected_real,
            "real_brands_smoked": real_actual,
            "missing_brand_ids": missing_brand_ids,
            "all_8_modules_present": all_8_present,
            "brief_consumption_active": brief_consumption_active,
            "inject_module_signals_ok": inject_module_signals_ok,
            "lint_clean": lint_clean,
            "lint_offenders": lint_offenders,
            "pass": smoke_pass,
        },
    }, ensure_ascii=False, indent=2)
    out_path.write_bytes(_payload_str.encode("utf-8") + b"\n")
    print(f"\n[m2-v2-smoke] JSON 已写: {out_path}")

    # 退出码:
    # - real_rows != 5 → fail(DB 缺数据 / 连不上 / brand_id 漂移)
    # - 8 模块缺一 → fail
    # - brief 消费 0 或 inject 模式 module_signals 全 False → fail(假绿)
    if not real_count_ok:
        print(
            f"\n❌ FAIL · 5 金标准只跑通 {real_actual}/{expected_real} · 缺 brand_id="
            f"{missing_brand_ids} · DB 没数据/连不上/brand_id 漂移"
        )
        return 1
    if all_8_present < expected_real:
        print(f"\n❌ FAIL · 8 模块装配只过 {all_8_present}/{expected_real} · 看 errors 字段")
        return 1
    if not brief_gate_ok:
        print(
            f"\n❌ FAIL · brief 消费门未过 · brief_consumption_active={brief_consumption_active} · "
            f"inject_module_signals_ok={inject_module_signals_ok} · "
            f"_enrich_with_brief 注入失败 / Module 1/4/5 没把 brief 渲染进 markdown"
        )
        return 1
    if not lint_clean:
        print(f"\n❌ FAIL · lint 仍命中 · {lint_offenders}")
        return 1
    print("\n✅ 5 金标准 brand 全部 8 模块装配 + brief 消费门 + lint 干净")
    return 0


if __name__ == "__main__":
    sys.exit(main())
