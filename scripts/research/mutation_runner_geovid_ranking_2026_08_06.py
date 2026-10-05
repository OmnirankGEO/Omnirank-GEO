# -*- coding: utf-8 -*-
"""全量变异 runner · WO_GEO_DOUYIN_RANKING_TEMPLATES_2026-08-06 v3

用法:
    python scripts/research/mutation_runner_geovid_ranking_2026_08_06.py            # 全跑
    python scripts/research/mutation_runner_geovid_ranking_2026_08_06.py --selftest # 只证锚点命中

🔴 三条纪律(都是本包踩过的):
  1. **先 `--selftest` 证锚点命中** —— 锚点撞到别处与锚点没命中表现相反、危害相同:
     后者会红,前者**永远不红**。
  2. **字节级读写 + 保持原行尾** —— `Path.write_text` 会按平台改行尾,
     曾把 diff 撑到虚高。
  3. **每轮跑完字节级还原并校验** —— 还原失败即中止,不留半改的源码。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

PAYLOAD = ROOT / "services" / "geo_douyin" / "ranking_payload.py"
ROUTER = ROOT / "services" / "geo_douyin" / "ranking_router.py"
GATES = ROOT / "services" / "geo_douyin" / "ranking_gates.py"
SOURCE = ROOT / "services" / "geo_douyin" / "ranking_source.py"
CARDS = ROOT / "services" / "geo_douyin" / "card_templates.py"
DISTILL = ROOT / "services" / "geo_douyin" / "topic_distiller.py"
GEN = ROOT / "services" / "geo_douyin" / "content_generator.py"
REDRAW = ROOT / "services" / "geo_douyin" / "redraw.py"
PROD = ROOT / "services" / "geo_douyin" / "production_task.py"
# [WO_271 · 2026-09-23] 已失效:变异目标 DouyinImagePost.tsx 于 09-08 删除(6b491ab23),本脚本不可再跑;
#   它打的那几把前端锁已改指现役文件(见各锁的 WO_271 注与 tests/RETIRED_TESTS.txt)。留作研究记录。
TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinImagePost.tsx"
V9 = ROOT / "writing" / "ranking_prompt_v9.py"
API_PY = ROOT / "api" / "geo_douyin_api.py"
CKB = ROOT / "services" / "client_knowledge.py"
SERIES = ROOT / "services" / "geo_douyin" / "series_plan.py"
CONTRACT = ROOT / "writing" / "competitor_name_contract.py"
SERVER = ROOT / "server.py"

T_PAYLOAD = "tests/test_geo_douyin_ranking_payload_2026_08_06.py"
T_ROUTER = "tests/test_geovid_ranking_router_2026_08_06.py"
T_GATES = "tests/test_geovid_ranking_gates_2026_08_06.py"
T_SOURCE = "tests/test_geovid_competitor_source_2026_08_06.py"
T_ADLAW = "tests/test_geovid_adlaw_same_source_2026_08_06.py"
T_REDRAW = "tests/test_geovid_redraw_keeps_layout_2026_08_06.py"
T_INDKEY = "tests/test_geo_douyin_industry_key_wiring_2026_08_06.py"
T_FAB = "tests/test_geovid_no_fabricated_competitor_2026_08_06.py"
T_WIRE = "tests/test_geovid_ranking_mainchain_wiring_2026_08_06.py"
T_SAMEROW = "tests/test_geovid_ranking_samerow_2026_08_06.py"
T_CAVEAT = "tests/test_geovid_caveat_a1_2026_08_06.py"
T_R3 = "tests/test_geovid_context_r3_2026_08_07.py"


@dataclass
class Mutant:
    name: str
    path: pathlib.Path
    old: bytes
    new: bytes
    suite: str


M: list[Mutant] = [
    # ── 冻结合同 · 归并/清洗/聚合 ────────────────────────────────────
    Mutant("归并把行业词也剥掉(P0 边界失守)", PAYLOAD,
           "    if _is_all_generic(core):\n        return _norm(raw)\n    return _norm(core)".encode(),
           ("    for t in _industry_tail_terms():\n"
            "        if core.endswith(t) and len(core) > len(t) + 1:\n"
            "            core = core[: -len(t)]\n            break\n"
            "    return _norm(core)  # MUTANT").encode(), T_PAYLOAD),
    Mutant("剥地域后不检查剩余长度(北京银行/上海银行 合成一家)", PAYLOAD,
           b"    if len(candidate) < _MIN_CORE_AFTER_ADMIN_STRIP or _is_all_generic(candidate):\n        return core",
           b"    if False:  # MUTANT\n        return core", T_PAYLOAD),
    Mutant("广告法退回纯子串(28% 误报)", PAYLOAD,
           b"            if not any(tail.startswith(b) for b in benign):",
           b"            if True:  # MUTANT", T_PAYLOAD),
    Mutant("七要素不齐也说综合排名", PAYLOAD,
           '    if contract.complete:\n        return f"综合排名第 {item.rank}"'.encode(),
           '    if True:  # MUTANT\n        return f"综合排名第 {item.rank}"'.encode(), T_PAYLOAD),
    Mutant("同族去重让位给名额(留痕被盖掉)", PAYLOAD,
           b"        fam = family_key(name)\n        if fam and fam in seen_family:",
           b"        fam = family_key(name)\n        if False:  # MUTANT", T_PAYLOAD),
    Mutant("清洗器放行第三方资质宣称", PAYLOAD,
           b"        if _THIRD_PARTY_RE.search(text):",
           b"        if False:  # MUTANT", T_PAYLOAD),
    Mutant("清洗器误杀中性特征词", PAYLOAD,
           b'_THIRD_PARTY_RE: Final = re.compile("|".join(_THIRD_PARTY_CLAIM_PATTERNS), re.IGNORECASE)',
           b'_THIRD_PARTY_RE: Final = re.compile(".", re.IGNORECASE)  # MUTANT', T_PAYLOAD),

    # ── 路由 · 两半式 ────────────────────────────────────────────────
    Mutant("无据也给 ranking form(D12④ 违反)", ROUTER,
           b"    form = FORM_MATRIX if len([c for c in (candidates or [])]) >= 4 else FORM_SCENARIO",
           b"    form = FORM_RANKING  # MUTANT", T_ROUTER),
    Mutant("手动覆盖能把无据说成有据", ROUTER,
           b"    form = verdict.form\n    if form == FORM_RANKING and not prefers:",
           b"    form = FORM_RANKING if force_ranking else verdict.form  # MUTANT\n    if False:", T_ROUTER),
    Mutant("给无据客户编一个名次", ROUTER,
           b'    return EvidenceVerdict(\n        False, form, None, "",',
           b'    return EvidenceVerdict(\n        False, form, 1, "",  # MUTANT', T_ROUTER),
    Mutant("家装变成硬关(整关行业)", ROUTER,
           b"    if ind in _CAPTION_RANKING_NEGATIVE:\n        return False",
           b'    if ind in _CAPTION_RANKING_NEGATIVE:\n        raise ValueError("blocked")  # MUTANT', T_ROUTER),
    Mutant("客户匹配退回精确串比", ROUTER,
           b'            if safe_merge_key(str(c.get("entity_name") or "")) != key:',
           b'            if str(c.get("entity_name") or "") != client_brand:  # MUTANT', T_ROUTER),
    Mutant("有据但非榜单行业时把实名次丢掉", ROUTER,
           b"        form = FORM_SCENARIO\n",
           b"        form = FORM_SCENARIO\n        verdict = EvidenceVerdict(False, form)  # MUTANT\n", T_ROUTER),

    # ── 闸 ──────────────────────────────────────────────────────────
    Mutant("R2 变成硬拦", GATES,
           b'gate="R2", level=LEVEL_A1, reason="entity_count_mismatch",',
           b'gate="R2", level=LEVEL_H0, reason="entity_count_mismatch",', T_GATES),
    Mutant("R5 文案回到宣称已切版式(说了没发生的事)", GATES,
           "        message=(f\"这批名次是 {observed_days_ago} 天前的,超过 {window_days} 天了 —— \"".encode(),
           "        message=(f\"这批名次是 {observed_days_ago} 天前的。已改用不依赖时效的版式 —— \"".encode(),
           T_GATES),
    Mutant("R5 降成不给任何可执行动作", GATES,
           "                 \"名次可能已经变了。想要新的,先跑一次监测再生成一次。\"),".encode(),
           "                 \"名次可能已经变了。\"),".encode(), T_GATES),
    Mutant("R8 只查两个举证要素", GATES,
           b'PROVENANCE_KEYS: Final = ("engine", "recommendation_rank", "extractor_version",\n                          "llm_model", "observed_at")',
           b'PROVENANCE_KEYS: Final = ("engine", "recommendation_rank")', T_GATES),
    Mutant("R1 不指向任何可执行路径(死胡同)", GATES,
           "点「看看」进详情页把这几张单独重做一下。".encode(),
           "请注意。".encode(), T_GATES),
    Mutant("序列化面偷偷长回死字段", GATES,
           b'SERIALIZED_KEYS: Final = ("gate", "message", "card_indices")',
           b'SERIALIZED_KEYS: Final = ("gate", "message", "card_indices", "actions")',
           T_GATES),
    Mutant("to_dict 又吐 level/reason(序列化常量 = 零信息)", GATES,
           b'            "gate": self.gate,\n            "message": self.message,',
           b'            "gate": self.gate, "level": self.level, "reason": self.reason,\n            "message": self.message,',
           T_GATES),
    Mutant("R7 误伤中性 caveat", GATES,
           'r"(差|烂|坑|骗|忽悠|不行|垃圾|劣质|偷工减料|没实力|不专业|翻车|跑路|失信)"'.encode(),
           'r"."'.encode(), T_GATES),

    # ── 竞品取数 ────────────────────────────────────────────────────
    Mutant("keywords 收窄撤掉", DISTILL,
           '            kw_clause = " AND keyword = ANY(%s)"'.encode(),
           '            kw_clause = ""'.encode(), T_SOURCE),
    Mutant("时效闸撤掉", DISTILL,
           "AND distilled_at >= NOW() - (%s || ' days')::interval\n".encode(), b"", T_SOURCE),
    Mutant("质量闸放进 rejected", DISTILL,
           'COMPETITOR_QUALITY_FLAGS = ("auto", "verified")'.encode(),
           'COMPETITOR_QUALITY_FLAGS = ("auto", "verified", "rejected", "degraded")'.encode(), T_SOURCE),
    Mutant("调用点不传 client_brand", DISTILL,
           'brand_id, kws, client_brand=(brand_name or ctx.brand_name or ""))'.encode(),
           "brand_id, kws)".encode(), T_SOURCE),
    Mutant("榜单候选默认排除客户", SOURCE,
           b"exclude_names: Optional[list[str]] = None,",
           b"exclude_names: Optional[list[str]] = (),", T_SOURCE),
    Mutant("举证链丢 extractor_version", SOURCE,
           b'"extractor_version": r.get("extractor_version"),', b"", T_SOURCE),

    # ── 广告法同源 ──────────────────────────────────────────────────
    Mutant("prompt 词表写死回去", GEN, b"{ad_law_block}",
           "- 禁绝对化用语:国家级/最高级/最佳/第一/顶级;".encode(), T_ADLAW),
    Mutant("通知变成阻断", GEN, b'"blocking": False,', b'"blocking": True,', T_ADLAW),
    Mutant("拿掉'先发'出口(变相硬拦)", GEN,
           '{"id": "publish_anyway", "label": "先发,我知道了", "type": "action"},'.encode(),
           b"", T_ADLAW),
    Mutant("to_dict 不暴露通知(退回零展示)", GEN,
           b'"ad_law_notice": ad_law_notice(self.ad_law_flags),\n', b"", T_ADLAW),

    # ── 重抽保留版式 ────────────────────────────────────────────────
    Mutant("重抽不传 layout_role", REDRAW, b"                    layout_role=layout_role,\n", b"", T_REDRAW),
    Mutant("重抽 total 写死", REDRAW,
           b"card_index=card_index + 1, total=total_cards,",
           b"card_index=card_index + 1, total=7,", T_REDRAW),
    Mutant("老数据不回推版式(删 import 留调用)", REDRAW,
           b"                    from services.geo_douyin.series_plan import plan_roles\n",
           b"                    pass  # MUTANT\n", T_REDRAW),
    Mutant("老数据回推整段砍掉", REDRAW,
           b"                if not layout_role:\n", b"                if False:  # MUTANT\n", T_REDRAW),
    Mutant("落库不存 layout_role", PROD,
           b'                "layout_role": spec.get("layout_role", ""),\n', b"", T_REDRAW),

    # ── industry_key 接线 ───────────────────────────────────────────
    # 🔴 `industry_key: brandIndustry,` 在 TSX 里有**两处**(蒸馏 :408 + 付费创建 :561)。
    #    锚点必须带上唯一的上文,否则 `replace(..., 1)` 改的是蒸馏那一处 ——
    #    锚点自检报「命中 2 次」时就是这个问题,`--selftest` 这道闸正是为它设的。
    Mutant("付费创建传死值 'general'", TSX,
           ("不补传就恒走默认版式。\n"
            "                    industry_key: brandIndustry,").encode(),
           ("不补传就恒走默认版式。\n"
            "                    industry_key: 'general',").encode(), T_INDKEY),
    Mutant("依赖数组漏掉(闭包锁死)", TSX,
           b", brandIndustry,\n        contentForm, rankingCount, rankingTemplate]);",
           b"]);", T_INDKEY),

    # ── 虚构竞品不可复活 ────────────────────────────────────────────
    Mutant("把虚构许可写回 prompt 字面量", V9,
           "- 禁止虚构公司".encode(), "- 中小型/区域性竞品可虚构".encode(), T_FAB),

    # ══ 2026-08-06 返工六项 ══════════════════════════════════════════
    # ① 主链接线(本次返工主体)
    Mutant("付费链不调编排(退回零调用方)", PROD,
           b"            ranking_outcome = await asyncio.to_thread(\n",
           b"            ranking_outcome = None; _unused = (\n", T_WIRE),
    Mutant("卡组型订单也触发榜单分支", PROD,
           b'        if str(content_form or "").strip() == RANKING_FORM:',
           b"        if True:  # MUTANT", T_WIRE),
    Mutant("冻结件不传进生成侧(假接线)", GEN,
           b"        ranking_block=ranking_prompt_block(ranking_plan),\n",
           b'        ranking_block="",\n', T_WIRE),
    Mutant("meta 不落 ranking(产出里看不到)", PROD,
           b'                **({"ranking": ranking_outcome.to_meta(),',
           b'                **({"_ranking_hidden": ranking_outcome.to_meta(),', T_WIRE),
    Mutant("闸结果不落库", PROD,
           b'                    "ranking_gates": [f.to_dict() for f in ranking_findings]}',
           b'                    "_gates_hidden": [f.to_dict() for f in ranking_findings]}', T_WIRE),
    Mutant("API 不把 content_form 传下去", API_PY,
           b"        content_form=str(req.content_form or '').strip(),\n", b"", T_WIRE),

    # ② 引擎与名次同行取数
    Mutant("退回解耦聚合(engines[0] 配 MIN(rank))", SOURCE,
           b'                "engine": str(r.get("best_engine") or ""),',
           b'                "engine": (([x for x in (r.get("engines") or []) if x] or [""])[0]),',
           T_SAMEROW),
    Mutant("rank_statement 用聚合层的 query 去拼", PAYLOAD,
           b'    q = str(src.get("query") or "").strip()',
           b"    q = str(contract.query or '').strip()", T_SAMEROW),
    Mutant("四元组缺元也照样断言引擎名次", PAYLOAD,
           b"    if not eng or not q or not observed:",
           b"    if False:  # MUTANT", T_SAMEROW),

    # ③ caveat 降 A1
    Mutant("缺 caveat 又丢整卡(级联整单判废)", GEN,
           b"        autofilled = not caveat\n        if autofilled:\n            caveat = CAVEAT_FALLBACK",
           b"        autofilled = False\n        if not caveat:\n            continue", T_CAVEAT),
    Mutant("兜底不留痕(前端分不出是不是自动补的)", GEN,
           b'            "caveat_autofilled": autofilled,\n', b"", T_CAVEAT),

    # ══ 2026-08-06 二次返工四项 ══════════════════════════════════════
    # ② R1 结构化比对(推倒中文正则猜名)
    Mutant("R1 退回猜名(把任意 entity 都当公司名判)", GATES,
           b"        ref = _slot_ref(slot)\n        if not ref:\n            continue",
           b"        ref = _slot_ref(slot) or _slot_display(slot)\n        if not ref:\n            continue",
           T_GATES),
    Mutant("R1 退回 H0(拦整单)", GATES,
           b'        gate="R1", level=LEVEL_A1,\n        reason="entity_display_mismatch"',
           b'        gate="R1", level=LEVEL_H0,\n        reason="entity_display_mismatch"', T_GATES),
    Mutant("R1 丢掉单卡作用域(退回整单级)", GATES,
           b"        card_indices=idx,", b"        card_indices=(),", T_GATES),
    Mutant("R1 名单为空时也报(恒红)", GATES,
           ("    if not keys and not norms:\n"
            "        # 名单为空 = 上游没给冻结件,此时任何判定都没有依据。**不报**。\n"
            "        return None").encode(),
           b"    if False:\n        return None", T_GATES),
    Mutant("R1 截断容错撤掉(长公司名被误判)", GATES,
           b"    return any(d.startswith(n) for d in display_norms)",
           b"    return False  # MUTANT", T_GATES),
    Mutant("R1 不查冒名顶替(声明 A 印 B 放行)", GATES,
           b"        if display and not _display_consistent(display, ref):",
           b"        if False:  # MUTANT", T_GATES),
    Mutant("声明字段被 clip 成 12 字(名单查不到)", GEN,
           b'            "entity_ref": str(c.get("entity_ref") or "").strip()[:60],',
           b'            "entity_ref": clip_text(c.get("entity_ref"), 12),', T_CAVEAT),
    Mutant("卡组型也往 prompt 里塞 entity_ref 字段", GEN,
           b"    return RANKING_CARD_FIELD if plan is not None else \"\"",
           b"    return RANKING_CARD_FIELD  # MUTANT", T_CAVEAT),

    # ③ blocking 死字段
    Mutant("blocking 字段复活", GATES,
           b'            "message": self.message,\n',
           b'            "message": self.message, "blocking": False,\n', T_GATES),
    Mutant("前缀容错没有下限(单字也能蹭过名单)", GATES,
           b"    if len(n) < _MIN_PREFIX_CHARS:\n        return False",
           b"    if False:  # MUTANT\n        return False", T_GATES),
    Mutant("前缀下限抬到 4(误杀「通力」这类 2 字真品牌)", GATES,
           b"_MIN_PREFIX_CHARS: Final = 2", b"_MIN_PREFIX_CHARS: Final = 4", T_GATES),

    # ④ 降级不静默
    Mutant("退回卡组型时不落降级留痕(静默换货)", ROUTER,
           b"                              fallback_reason=reason, client_brand=client_brand,\n"
           b"                              request=req)",
           b'                              fallback_reason="", client_brand=client_brand,\n'
           b"                              request=req)",
           T_WIRE),
    Mutant("做成场景版式却不算降级(不告诉用户)", ROUTER,
           b"        elif not routed[\"has_client_evidence\"]:",
           b"        elif False:  # MUTANT", T_WIRE),
    Mutant("降级告知没有出口", ROUTER,
           '    keep = {"id": "keep_current", "label": "就用这版", "type": "action"}'.encode(),
           b'    keep = {"id": "_gone", "label": "x", "type": "action"}', T_WIRE),
    Mutant("nav 出口不带 href(前端只能猜路由)", ROUTER,
           '    supplement = {"id": "supplement_candidates", "label": "去跑监测攒候选",\n                  "type": "nav", "href": "/monitoring"}'.encode(),
           '    supplement = {"id": "supplement_candidates", "label": "去跑监测攒候选",\n                  "type": "nav"}'.encode(), T_WIRE),
    Mutant("降级文案改回合规腔", ROUTER,
           "            \"这个行业还没攒够可以点名的同行数据,这次做成了卡组版式。\"".encode(),
           "            \"证据不足以出榜,存在合规风险,已降级。\"".encode(), T_WIRE),
    # 🔴 这条原来写成 `if not reason: return None` → `if False:`,**是个等价变异**:
    #    下面 `msg = table.get(reason)` 对空 reason 返回 None,第二道 `if not msg`
    #    照样兜住,行为一个字没变。等价变异"存活"证明不了锁没判别力 ——
    #    改成给未知 reason 兜一句默认文案,这才真的会在没降级时弹提示。
    Mutant("没降级也弹提示(恒扰)", ROUTER,
           b"    msg = table.get(reason)",
           '    msg = table.get(reason) or "已按可用数据生成"'.encode(), T_WIRE),
    Mutant("家数旋钮不接线(选了没反应)", PROD,
           b"                want=int(ranking_entity_count or 0),",
           b"                want=0,", T_WIRE),
    Mutant("版式旋钮不接线", PROD,
           b'                force_template=str(ranking_template or "").strip(),',
           b'                force_template="",', T_WIRE),
    Mutant("force_ranking 传 False 而不是 None(反把默认为正的行业关掉)", PROD,
           b"                force_ranking=True if ranking_force else None,",
           b"                force_ranking=bool(ranking_force),", T_WIRE),

    # ══ 2026-08-07 R3(上下文共用 / 豆包目标面 / 构造式企业卡 / 继承)════
    # P1-2 引擎轴
    Mutant("目标引擎写成拉丁字面量(库里是中文 → 恒 0 行 → 恒降级)", ROUTER,
           'TARGET_ENGINE: Final = "豆包"'.encode(),
           b'TARGET_ENGINE: Final = "doubao"', T_R3),
    Mutant("引擎名另建第二份映射(不复用 SSOT)", SOURCE,
           ("    from services.research_monitor.answer_entity_extractor "
            "import _ENGINE_CANONICAL\n").encode(),
           "    _ENGINE_CANONICAL = {'doubao': '豆包'}\n".encode(), T_R3),
    Mutant("把偏好写成过滤(候选收窄成单引擎 → 撞共识闸)", SOURCE,
           b"                      (LOWER(e.engine) <> ALL(%s)),",
           b"                      (LOWER(e.engine) = ANY(%s)),", T_R3),
    Mutant("不传目标引擎偏好(名次退回按最小值取)", ROUTER,
           b"                                        prefer_engine=TARGET_ENGINE)",
           b'                                        prefer_engine="")', T_R3),
    Mutant("哨兵含 NUL(psycopg2 直接拒 → 异常被吞成无候选)", SOURCE,
           b'else ["__no_engine_preference__"]',
           ('else ["' + chr(0) + '__no_preference__"]').encode(), T_R3),

    # P1-3 再次创作继承
    Mutant("再次创作不继承冻结快照(付费重做变普通图文)", API_PY,
           b'    inherited = RankingRequest.from_meta(post.get("generation_meta") or {})',
           b"    inherited = None  # MUTANT", T_R3),
    Mutant("三态退化成两态(没改与改成自动分不开)", API_PY,
           b"    ranking_template: Optional[str] = None",
           b'    ranking_template: str = ""', T_R3),
    Mutant("快照存实际家数而不是用户要的家数(一路缩水)", ROUTER,
           b"        content_form=requested_form, entity_count=int(want or 0),",
           b"        content_form=requested_form, entity_count=0,", T_R3),

    # P1-4 构造式企业卡
    Mutant("entity_ref 退回由模型填(省略即可绕过)", GEN,
           b'            card["entity_ref"] = str(plan["entity_name"])\n',
           b"            pass  # MUTANT\n", T_R3),
    Mutant("榜单形态不走构造式职责表", GEN,
           b"    if _ranking_names:\n",
           b"    if False:  # MUTANT\n", T_R3),
    Mutant("张数装不下也硬做(悄悄少做几家)", ROUTER,
           b'            if slots < routed["entity_count_min"]:',
           b"            if False:  # MUTANT", T_R3),
    Mutant("固定卡数改掉(封面/收尾不再各占一张)", SERIES,
           b"RANKING_FIXED_CARDS: int = 2",
           b"RANKING_FIXED_CARDS: int = 0", T_R3),

    # P1-1 竞品共用
    Mutant("fictional 竞品也当人工确认名单(把编的公司放进榜单)", CKB,
           'REAL_COMPETITOR_MODES: tuple = ("real", "semi", "evidence_only")'.encode(),
           'REAL_COMPETITOR_MODES: tuple = ("real", "semi", "evidence_only", "fictional")'.encode(),
           T_R3),
    Mutant("草稿报价也算数", CKB,
           'CONFIRMED_QUOTE_STATUSES: tuple = ("confirmed", "paid")'.encode(),
           'CONFIRMED_QUOTE_STATUSES: tuple = ("confirmed", "paid", "draft")'.encode(), T_R3),
    Mutant("血缘退回按品牌取最近一单(猜关联)", CKB,
           b"    if kws:\n        # \xf0\x9f\x94\xb4 \xe7\xb2\xbe\xe7\xa1\xae\xe8\xa1\x80\xe7\xbc\x98",
           b"    if False:\n        # MUTANT", T_R3),
    Mutant("人工名单变成加法而不是上界", ROUTER,
           b"            pool = inside",
           b"            pool = pool + inside  # MUTANT", T_R3),
    # ── 2026-08-08 本包:行业键归一 + 名单对不齐时回落 ──────────────────
    Mutant("收窄不设门槛(口径一不齐就把好候选全砍了)", ROUTER,
           b"            if len(inside) >= ENTITY_COUNT_MIN:",
           b"            if True:  # MUTANT", T_R3),
    Mutant("口径没对齐这件事不落库(只活在日志里)", ROUTER,
           b'            "confirmed_allowlist": dict(self.allowlist or {}),\n',
           b"", T_R3),
    Mutant("已删的降级原因回潮(死元数据第五次)", ROUTER,
           b'FALLBACK_CARD_BUDGET: Final = "card_budget_too_small"',
           b'FALLBACK_CARD_BUDGET: Final = "card_budget_too_small"\n'
           b'FALLBACK_CONFIRMED_NO_EVIDENCE: Final = "confirmed_list_lacks_evidence"',
           T_R3),
    Mutant("行业键不归一(自由文本直查候选池)", SOURCE,
           b"    ind = normalize_industry_key(ind)",
           b"    ind = ind  # MUTANT", T_R3),
    Mutant("只在取数处归一,冻结件仍记自由文本", ROUTER,
           b"            industry_key=ind_key, city=city,",
           b"            industry_key=industry_key, city=city,", T_R3),
    Mutant("版式路由仍吃自由文本(面×行业判定恒走默认)", ROUTER,
           b"        routed = route_template(pool, industry_key=ind_key, city=city,",
           b"        routed = route_template(pool, industry_key=industry_key, city=city,",
           T_R3),
    Mutant("配色查表退回裸键(行业配色恒兜底)", CARDS,
           b"    return _INDUSTRY_PALETTE.get(_ikey(industry_key), _PALETTE_FALLBACK)",
           b'    return _INDUSTRY_PALETTE.get(str(industry_key or "").strip(), _PALETTE_FALLBACK)',
           T_INDKEY),
    Mutant("首图钩子查表退回裸键(钩子恒兜底)", CARDS,
           b"    return INDUSTRY_HOOK.get(_ikey(industry_key), DEFAULT_HOOK)",
           b'    return INDUSTRY_HOOK.get(str(industry_key or "").strip(), DEFAULT_HOOK)',
           T_INDKEY),
    # ── 2026-08-08 上游:画像自陈无主体 → 自动排除 ──────────────────────
    Mutant("画像落地后不判主体(脏候选继续攒)", SERVER,
           b"                        if profile_lacks_subject(profile_text):",
           b"                        if False:  # MUTANT", T_R3),
    Mutant("判了却不标排除(判据白跑)", SERVER,
           b'                            comp["excluded"] = True',
           b'                            comp["excluded"] = False', T_R3),
    Mutant("只判品类词,漏掉「查无此企业」那一形态", CONTRACT,
           b"    return profile_describes_a_category(text) or profile_denies_the_entity(text)",
           b"    return profile_describes_a_category(text)  # MUTANT", T_R3),
    Mutant("榜单链又不过滤 excluded(把人工排除的捞回榜单)", CKB,
           b'            if isinstance(it, dict) and it.get("excluded"):',
           b'            if isinstance(it, dict) and False:  # MUTANT', T_R3),
    # ── 2026-08-08 B 层:画像可引用(门槛/剥词/分档/闸放行)──────────────
    Mutant("可引用门槛退回 source_count(会放行品类词)", CONTRACT,
           b"    if not is_name_verified(item):\n        return False",
           b"    if int((item or {}).get('source_count') or 0) < 2:  # MUTANT\n        return False",
           T_R3),
    Mutant("主体判定失效(无主体的也算可引用)", CONTRACT,
           b"    return not profile_lacks_subject(text)",
           b"    return True  # MUTANT", T_R3),
    Mutant("绝对化用语不剥就递给模型", SERIES,
           b'        terms = [str(w) for w in (legal_pack().get("ad_law") or ()) if str(w or "")]',
           b"        terms = []  # MUTANT", T_R3),
    Mutant("可引用与不可引用不分档(文案一刀切)", SERIES,
           b'                if (ent or {}).get("citable"):',
           b"                if False:  # MUTANT", T_R3),
    Mutant("R9 连可引用的也判(自相矛盾的闸)", GATES,
           b'            if isinstance(v, dict) and not v.get("citable")',
           b"            if isinstance(v, dict)", T_R3),
    Mutant("读取层不报可引用(全判成不可引用)", CKB,
           b'                "profile_citable": _profile_citable(ev),',
           b'                "profile_citable": False,', T_R3),
    # ── 2026-08-08 A 层:竞品画像进内容创作 ────────────────────────────
    Mutant("画像在读取层又被丢掉(回到只拿名字)", CKB,
           b'                "profile": str(ev.get("profile") or ev.get("desc") or "").strip(),',
           b'                "profile": "",', T_R3),
    Mutant("画像到不了企业卡 prompt", SERIES,
           b'            ent = pmap.get(safe_merge_key(str(item["entity_name"])))',
           b"            ent = None  # MUTANT", T_R3),
    Mutant("画像整段给出去(不截断 = 邀请模型搬运)", SERIES,
           b"PROFILE_CONTEXT_CHARS: int = 120",
           b"PROFILE_CONTEXT_CHARS: int = 4000", T_R3),
    Mutant("prompt 不再明写「不许照抄」", SERIES,
           "            \"🔴 上面的「背景」是内部选材参考,**一个字都不许照抄进卡面**;\"".encode(),
           "            \"背景仅供参考。\"".encode(), T_R3),
    Mutant("R9 阈值抬到失效(整段搬运也放行)", GATES,
           b"VERBATIM_MAX_CHARS: Final = 16",
           b"VERBATIM_MAX_CHARS: Final = 400", T_R3),
    Mutant("R9 只看写死字段(搬运发生在别的字段就漏掉)", GATES,
           b"    _walk(slot)\n    return \"\\n\".join(buf)",
           b"    _walk(slot.get(\"entity\"))  # MUTANT\n    return \"\\n\".join(buf)", T_R3),
    Mutant("付费链不把画像传给闸(闸在·判据不在)", PROD,
           b'                entity_profiles=getattr(ranking_plan, "profiles", None),\n',
           b"", T_R3),
    Mutant("生成器不把画像传给职责表", GEN,
           b'                                                profiles=getattr(ranking_plan,\n'
           b'                                                                 "profiles", None))',
           b"                                                profiles=None)", T_R3),
    Mutant("画像落进 meta(等于对外发布未核验的第三方描述)", ROUTER,
           b'            "confirmed_allowlist": dict(self.allowlist or {}),',
           b'            "confirmed_allowlist": dict(self.allowlist or {}),\n'
           b'            "profiles": dict(self.profiles or {}),', T_R3),
    Mutant("B端判定退回裸键(B·C 端分不出来)", CARDS,
           b"    return _ikey(industry_key) in _B_SIDE_INDUSTRIES",
           b'    return str(industry_key or "").strip() in _B_SIDE_INDUSTRIES',
           T_INDKEY),
    Mutant("entity_id 不与证据表同键空间(永远 join 不上)", CKB,
           b"            key = build_answer_entity_key(name)",
           b"            key = name  # MUTANT", T_R3),

    # 窄 DTO
    Mutant("窄 DTO 直接把整块 meta 交出去(内部字段泄漏)", ROUTER,
           b"    gates = [g for g in (meta.get(\"ranking_gates\") or []) if isinstance(g, dict)]\n    return {",
           b"    gates = [g for g in (meta.get(\"ranking_gates\") or []) if isinstance(g, dict)]\n    return {**block,", T_R3),
    Mutant("详情端点不给窄 DTO(前端只能去读原始 meta)", API_PY,
           b'        "ranking": ranking_summary(post.get("generation_meta")),\n', b"", T_R3),
]


def _purge_pycache() -> None:
    """🔴 每次改完源码必须清 `__pycache__`。

    CPython 按 **(源文件大小, mtime)** 判缓存是否失效。变异 runner 在同一个文件上
    反复写入/还原,两次写入的 mtime 可能落在同一时间戳粒度内、大小又碰巧相同 ——
    于是**解释器继续用变异版编译出来的 .pyc**。

    本包实测踩到了:38 条跑完并字节级还原后,`test_legacy_card_without_layout_role`
    在**干净源码**上仍然红,清掉 __pycache__ 立刻绿。
    这个坑是双向的:既可能把"该杀的"报成存活(假绿),也可能把干净代码报成红。
    """
    for d in ROOT.rglob("__pycache__"):
        for f in d.glob("*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass


def _run(suite: str) -> int:
    _purge_pycache()
    import os
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"      # 双保险:这一跑根本不落 .pyc
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", suite, "-q", "--no-header"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    m = re.search(r"(\d+) (?:failed|error)", r.stdout + r.stderr)
    return int(m.group(1)) if m else 0


def selftest() -> int:
    """只证锚点命中 —— 不改任何文件。"""
    bad = 0
    for mu in M:
        raw = mu.path.read_bytes()
        n = raw.count(mu.old)
        if n != 1:
            print(f"  ✗ 锚点命中 {n} 次(应为 1):{mu.name}")
            bad += 1
    print(f"锚点自检:{len(M) - bad}/{len(M)} 唯一命中")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    if selftest():
        print("锚点自检未过,拒绝跑变异(判据不可信)")
        return 1

    suites = sorted({mu.suite for mu in M})
    for s in suites:
        if _run(s):
            print(f"基线红:{s} —— 先修基线")
            return 1
    print(f"基线全绿({len(suites)} 个套件)\n")

    killed = survived = 0
    for mu in M:
        orig = mu.path.read_bytes()
        mu.path.write_bytes(orig.replace(mu.old, mu.new, 1))
        try:
            f = _run(mu.suite)
        finally:
            mu.path.write_bytes(orig)
            assert mu.path.read_bytes() == orig, f"源码未还原:{mu.path}"
        if f:
            print(f"  ✅ 杀死 ({f:>2} failed)  {mu.name}")
            killed += 1
        else:
            print(f"  ❌ 存活          {mu.name}   ← 锁没判别力")
            survived += 1
    print(f"\n变异 {killed}/{len(M)} 杀死,{survived} 存活")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
