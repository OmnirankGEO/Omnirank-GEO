"""变异 runner · GEO 抖音图文管线 v1

工单 §8:「变异 runner 带 --selftest」。把判据改坏,对应的锁必须转红;
变异存活 = 那条锁没有判别力(= 假绿)。

🔴 三条踩过的坑固化在这里:
  1. 变异必须【先证明锚点命中源码】,否则 replace 不生效 = 假的"全杀";
  2. 跑完必须【断言源码逐字节恢复】,否则会把变异体留在仓里;
  3. 基线必须先绿 —— 红的东西再改还是红,那种"被杀"没有意义。

用法:
    python scripts/research/mutation_runner_geovid.py            # 全部
    python scripts/research/mutation_runner_geovid.py --selftest # 核验 runner 自身
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[2]

ASR_PROBE = ROOT / "scripts" / "research" / "douyin_adopted_asr_probe.py"
TITLE_ENGINE = ROOT / "services" / "geo_douyin" / "title_engine.py"
IMAGE_PIPE = ROOT / "services" / "geo_douyin" / "image_pipeline.py"
PROD_TASK = ROOT / "services" / "geo_douyin" / "production_task.py"
PUB_ADAPTER = ROOT / "services" / "geo_douyin" / "publish_adapter.py"
MHZ_API = ROOT / "api" / "meijiehezi_api.py"
GEO_API = ROOT / "api" / "geo_douyin_api.py"
# 🔴 发布路径 2026-08-02 整页搬进三栏详情页,旧 DouyinPublishPanel.tsx 已删。
#    变异锚点必须跟着实现走 —— 指着已删文件时 runner 会在 --selftest 就报锚点失效,
#    这比"锚点还在旧文件上、变异永远杀不掉"好得多(后者是假绿)。
PUB_PANEL = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"
PRICING_SQL = ROOT / "db" / "migration_019_geo_douyin_pricing_390_2026_08_02.sql"
PORTAL = ROOT / "frontend" / "src" / "pages" / "Portal" / "PortalDashboard.tsx"
CARD_TPL = ROOT / "services" / "geo_douyin" / "card_templates.py"
CONTENT_GEN = ROOT / "services" / "geo_douyin" / "content_generator.py"
KB_CTX = ROOT / "services" / "geo_douyin" / "knowledge_context.py"
REDRAW = ROOT / "services" / "geo_douyin" / "redraw.py"
KB_CONS = ROOT / "services" / "geo_douyin" / "kb_consistency.py"
IMG_CLIENT = ROOT / "services" / "marketing" / "image_client.py"
GITIGNORE = ROOT / ".gitignore"
TRACKER = ROOT / "tools" / "llm_call_tracker.py"
GEO_DB = ROOT / "db" / "geo_douyin_db.py"
# 2026-08-05 蒸馏改异步:节流/扣费从端点搬进后台任务模块,锚点跟着搬
DISTILL_TASK = ROOT / "services" / "geo_douyin" / "distill_task.py"
DISTILL_SQL = ROOT / "db" / "migration_025_geo_douyin_distill_tasks_2026_08_05.sql"
DETAIL_TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"
MANIFEST = ROOT / "db" / "migration_manifest.py"

T_TITLE = "tests/test_geo_douyin_title_engine.py"
T_IMAGE = "tests/test_geo_douyin_image_pipeline.py"
T_BILL = "tests/test_geo_douyin_production_billing.py"
T_PUB = "tests/test_geo_douyin_publish_adapter.py"
T_NAME = "tests/test_svideo_media_name_resolution.py"
T_FLOW = "tests/test_geo_douyin_publish_flow.py"
T_OWNER = "tests/test_geo_douyin_pricing_and_portal_hint.py"
T_TPL = "tests/test_geo_douyin_card_templates.py"
T_UI = "tests/test_geo_douyin_detail_ui.py"
T_RETRY = "tests/test_image_client_retry.py"
T_ASYNC = "tests/test_geo_douyin_async_and_entry.py"
T_FOLLOW = "tests/test_geo_douyin_followup_2026_08_03.py"
T_PARTIAL = "tests/test_geo_douyin_partial_success.py"
MODULE_MAP = ROOT / "auth" / "module_mapping.py"
SETTLE = ROOT / "services" / "geo_douyin" / "settlement.py"
SV_PANEL = ROOT / "frontend" / "src" / "pages" / "Publishing" / "ShortVideoPanel.tsx"

TASK_PROG = ROOT / "services" / "geo_douyin" / "task_progress.py"
# [WO_271 · 2026-09-23] 已失效:变异目标 DouyinImagePost.tsx 于 09-08 删除(6b491ab23),本脚本不可再跑;
#   它打的那几把前端锁已改指现役文件(见各锁的 WO_271 注与 tests/RETIRED_TESTS.txt)。留作研究记录。
ENTRY_TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinImagePost.tsx"
# ── 2026-08-03 全量包(一条流水线 v2 + 杂志级连续组图规范)新增目标 ──
SERIES_PLAN = ROOT / "services" / "geo_douyin" / "series_plan.py"
PRICING = ROOT / "services" / "geo_douyin" / "pricing.py"
DISTILL = ROOT / "services" / "geo_douyin" / "topic_distiller.py"
CORPUS_DB = ROOT / "db" / "douyin_corpus_db.py"
CLIENT_KB = ROOT / "services" / "client_knowledge.py"
WORKSPACE_TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingWorkspace.tsx"
HALL_TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
SERVER_PY = ROOT / "server.py"
T_V2 = "tests/test_geo_one_pipeline_v2_locks.py"
# ── 2026-08-03 收尾包(v3)新增目标 ──
# 🔴 新模块必须同时进这里和接线检查表 —— 同一个坑已经踩过三次。
OCR_QA = ROOT / "services" / "geo_douyin" / "ocr_qa.py"
GEO_CONFIG = ROOT / "services" / "geo_douyin" / "config.py"
T_V3 = "tests/test_geo_one_pipeline_v3_locks.py"
CONTENT_PLAN = ROOT / "services" / "geo_douyin" / "content_plan.py"
T_PIPE1 = "tests/test_geo_content_one_pipeline.py"
TASK_HOOK = ROOT / "frontend" / "src" / "hooks" / "useDouyinPostTask.ts"
# ── 2026-08-04 Owner 看图包 ──
DOUYIN_API = ROOT / "api" / "geo_douyin_api.py"
STYLE_PICKER = ROOT / "frontend" / "src" / "components" / "writing" / "CardStylePicker.tsx"
IMAGE_PIPE_2 = ROOT / "services" / "geo_douyin" / "image_pipeline.py"

# 2026-08-06 生产堵塞排查包(v5)
T_V5 = "tests/test_geo_one_pipeline_v5_locks.py"
T_HOTFIX = "tests/test_douyin_brand_name_key_2026_08_05.py"

ENV = {
    **os.environ,
    "PYTHONIOENCODING": "utf-8",
    "TEST_DATABASE_URL": os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://testuser:testpw@127.0.0.1:55510/geovid_test_0801"),
}
ENV["DATABASE_URL"] = ENV["TEST_DATABASE_URL"]

_ASR_PRED = (
    "    t = text.lstrip()\n"
    "    return t.startswith(_ASR_ERROR_PREFIXES) or any(\n"
    "        m in text for m in _ASR_ERROR_MARKERS\n"
    "    )"
)


@dataclass(frozen=True)
class Mutant:
    name: str
    target: Path
    old: str
    new: str
    verify: str          # "selftest" 或 pytest 文件路径


MUTANTS: List[Mutant] = [
    # ── ASR fail-closed 检测器(研究脚本)──
    Mutant("A1 ASR 检测器恒真", ASR_PROBE, _ASR_PRED,
           "    return True  # MUTANT", "selftest"),
    Mutant("A2 ASR 检测器恒假(=最初那个假绿 bug)", ASR_PROBE, _ASR_PRED,
           "    return False  # MUTANT", "selftest"),
    Mutant("A3 丢掉 'Error:' 前缀判定", ASR_PROBE,
           '_ASR_ERROR_PREFIXES = ("Error:", "error:", "Exception:", "Traceback")',
           '_ASR_ERROR_PREFIXES = ("__nope__",)  # MUTANT', "selftest"),
    Mutant("A4 丢掉 dict 错误码分支", ASR_PROBE,
           '        if parsed.get("code") or parsed.get("error") or parsed.get("status") in (\n'
           '            "timeout", "failed", "error"\n        ):',
           "        if False:  # MUTANT", "selftest"),
    Mutant("A5 清空 marker 表", ASR_PROBE,
           '_ASR_ERROR_MARKERS = (\n    "InvalidApiKey", "API-key", \'"code"\', "request_id",\n'
           '    "Throttling", "AccessDenied", "Arrearage",\n)',
           "_ASR_ERROR_MARKERS = ()  # MUTANT", "selftest"),

    # ── 标题引擎:城市变体必须错开(真踩过的 bug)──
    Mutant("T1 城市矩阵退回加权池步进(=原始 bug:5 城同句式)", TITLE_ENGINE,
           "        tpl = templates[(int(start_index) + picked) % len(templates)]",
           "        tpl = _TEMPLATES[0]  # MUTANT", T_TITLE),
    Mutant("T2 榜单权重压过选型问法(违背 §6 实证)", TITLE_ENGINE,
           '              "rank", 8, True),',
           '              "rank", 500, True),  # MUTANT',
           T_TITLE),
    Mutant("T3 标题不做硬上限截断", TITLE_ENGINE,
           "    limit = _title_hard_limit()\n    if len(raw) <= limit:\n        return raw, False",
           "    return raw, False  # MUTANT", T_TITLE),
    Mutant("T4 hashtag 不去重", TITLE_ENGINE,
           "        if t and t not in seen:", "        if t:  # MUTANT", T_TITLE),

    # ── 图文生产:两条红线 ──
    Mutant("I1 OSS 上传不走 to_thread(阻塞事件循环)", IMAGE_PIPE,
           "    return await asyncio.to_thread(_put)",
           "    return _put()  # MUTANT", T_IMAGE),
    Mutant("I2 部分失败也算全成功", IMAGE_PIPE,
           "        return bool(self.cards) and all(c.ok for c in self.cards)",
           "        return True  # MUTANT", T_IMAGE),
    Mutant("I3 OSS key 不带随机(会撞号)", IMAGE_PIPE,
           '    return (f"geo_douyin/{int(post_id)}/card_{int(idx)}_"\n'
           '            f"{int(time.time())}_{secrets.token_hex(4)}.{normalized}")',
           '    return f"geo_douyin/{int(post_id)}/card_{int(idx)}.{normalized}"  # MUTANT',
           T_IMAGE),

    # ── 计费闭合(资金面)──
    Mutant("B1 失败不退款", PROD_TASK,
           "            if freeze_id:\n                await release_freeze(freeze_id=freeze_id, task_ref=task_ref,\n"
           '                                     reason=f"抖音图文帖生产失败:{stage}")\n'
           "                outcome.refunded = True",
           "            pass  # MUTANT", T_BILL),
    Mutant("B2 成功不扣费", PROD_TASK,
           "        if freeze_id:\n            await commit_freeze(freeze_id=freeze_id, task_ref=task_ref,\n"
           '                                reason="抖音图文帖生产完成")',
           "        pass  # MUTANT", T_BILL),
    # B3 语义已随 §15 改写:`if not batch.all_ok` 之后不再是"整条作废",
    # 而是进门槛判定。变异改判据 → 应落到部分成功那套锁上。
    Mutant("B3 跳过成品门槛判定(达不达标都进补齐中)", PROD_TASK,
           "            if not is_deliverable(enriched):",
           "            if False:  # MUTANT", T_BILL),
    Mutant("B4 task_ref 固定(撞号→退错单)", PROD_TASK,
           '    return f"geo_douyin_post:{int(post_id)}:{int(time.time())}:{secrets.token_hex(3)}"',
           '    return "FIXED_REF"  # MUTANT', T_BILL),
    Mutant("B5 兜底 except 吞掉不退款", PROD_TASK,
           "    except Exception as e:  # noqa: BLE001 - 兜底:任何未预期异常也必须退积分\n"
           '        return await _fail("unexpected", f"{type(e).__name__}: {str(e)[:140]}")',
           "    except Exception as e:  # MUTANT\n        outcome.error = str(e); return outcome",
           T_BILL),

    # ── 发布接入 ──
    Mutant("P1 频控全放行", PUB_ADAPTER,
           "    v.ok = not v.violations", "    v.ok = True  # MUTANT", T_PUB),
    Mutant("P2 城市变体规则失效", PUB_ADAPTER,
           "    if len(ids) > 1 and gap > 1 and distinct_cities < min(gap, len(ids)):",
           "    if False:  # MUTANT", T_PUB),
    Mutant("P3 产物域白名单不校验", PUB_ADAPTER,
           "        if not _is_allowed_svideo_media_url(url):",
           "        if False:  # MUTANT", T_PUB),
    Mutant("P4 单张失败不整体失败", PUB_ADAPTER,
           '            return PreparedMedia(\n                ok=False,\n'
           '                error=_neutral(f"第 {i+1} 张图上传失败：{e}"))',
           "            continue  # MUTANT", T_PUB),
    Mutant("P5 用户可见错误不做供应商脱敏", PUB_ADAPTER,
           "    from services.publish_channel_privacy import scrub_text\n    return scrub_text(text)",
           "    return text  # MUTANT", T_PUB),
    Mutant("P6 图文模式误传 article_type=1", PUB_ADAPTER,
           "ARTICLE_TYPE_IMAGE_NOTE = 3", "ARTICLE_TYPE_IMAGE_NOTE = 1  # MUTANT", T_PUB),

    # ── media_name 缺陷② ──
    Mutant("N1 退回只信客户端取名(=缺陷②未修)", MHZ_API,
           '"media_name": _name_map.get(int(mid)) or _client_name,',
           '"media_name": _client_name,  # MUTANT', T_NAME),

    # ── 一键发布链路 ──
    Mutant("G1 can_publish 漏掉发布侧闸(只看制作闸)", GEO_API,
           "    can_publish = make_on and upload_on and publish_on",
           "    can_publish = make_on  # MUTANT", T_FLOW),
    Mutant("G2 读不到闸门默认放行(丢 fail-closed)", GEO_API,
           "        upload_on = publish_on = False",
           "        upload_on = publish_on = True  # MUTANT", T_FLOW),
    # 🔴 G3/G4/G5 原本打在**详情页内嵌发布链**上,而那条链 2026-08-03 已整体删除
    #    (Owner 裁定:发布必须用发布中心完整池)。锚点跟着实现走 ——
    #    selftest 当场报"未命中源码",这正是它该有的样子;留着不改就是锁在死代码上。
    Mutant("G3 闸关也能点「去发布投放」", PUB_PANEL,
           "    const jumpBlocked = !canPublish || notReady || completing;",
           "    const jumpBlocked = notReady;  // MUTANT", T_FLOW),
    Mutant("G4 归因锚端点被删(发到哪去了永远查不到)", GEO_API,
           '@router.post("/publish-result")',
           '@router.post("/__removed_publish_result__")  # MUTANT', T_FLOW),
    Mutant("G5 预填不切图文模式(article_type 会是 1=发视频)", SV_PANEL,
           "      setPublishMethod('article');",
           "      // MUTANT 不切模式", T_FLOW),

    # ── 发布流向重定(Owner 2026-08-03 点名的三条变异对)──
    Mutant("N1 跳转不带 post_id(发布中心什么都预填不了)", PUB_PANEL,
           "        navigate(`/publish?media_type=svideo&geo_post_id=${postId}`);",
           "        navigate('/publish?media_type=svideo');  // MUTANT", T_FLOW),
    Mutant("N2 预填丢图(跳过去是张空表单,用户得重传 9 张)", SV_PANEL,
           "      setNoteImages(imgs.slice(0, MAX_NOTE_IMAGES));",
           "      // MUTANT 丢图", T_FLOW),
    Mutant("N3 内嵌下单路径复活(又绕开完整池)", PUB_PANEL,
           "    const goPublish = () => {",
           "    const _revived = '/api/meijiehezi/short-video/publish';  // MUTANT",
           T_FLOW),
    Mutant("N4 接收侧丢掉向后兼容(不带参数也去打接口)", SV_PANEL,
           "    if (!geoPostId || geoPrefilled.current) return;",
           "    if (geoPrefilled.current) return;  // MUTANT", T_FLOW),
    Mutant("N5 补齐中的组也放行去投放(缺图发出去 + 钱没结算)", PUB_PANEL,
           "    const jumpBlocked = !canPublish || notReady || completing;",
           "    const jumpBlocked = !canPublish || notReady;  // MUTANT", T_FLOW),
    Mutant("N6 后端不拦补齐中(前端 checklist 可被绕过)", GEO_API,
           "    if post.get(\"status\") == STATUS_COMPLETING:",
           "    if False:  # MUTANT", T_FLOW),
    Mutant("G6 闸关文案改成供应商名(零暴露被破)", GEO_API,
           'reason = "外部发布通道暂未开放，可以先做内容并下载成品包"',
           'reason = "媒介盒子暂未开放"  # MUTANT', T_FLOW),

    # ── Owner 2026-08-02 拍板值 ──
    Mutant("O1 制作费被改回 260(Owner 08-02 拍板 390)", PRICING_SQL,
           "VALUES ('geo_douyin_image_post', 'GEO 图文制作（单条）', 390, 3.00, FALSE)",
           "VALUES ('geo_douyin_image_post', 'GEO 图文制作（单条）', 260, 2.00, FALSE)  -- MUTANT",
           T_OWNER),
    Mutant("O2 首次档 cost_compute 偏离 article_gen", PRICING_SQL,
           "390, 3.00, FALSE)", "390, 1.00, FALSE)  -- MUTANT", T_OWNER),
    Mutant("O3 制作费误设成只扣充值积分(发布费口径)", PRICING_SQL,
           "390, 3.00, FALSE)", "390, 3.00, TRUE)  -- MUTANT", T_OWNER),
    Mutant("O3b 重新生成档被调到比首次还贵", PRICING_SQL,
           "VALUES ('geo_douyin_image_post_regen', 'GEO 图文重新生成（单条）', 260, 3.00, FALSE)",
           "VALUES ('geo_douyin_image_post_regen', 'GEO 图文重新生成（单条）', 520, 3.00, FALSE)  -- MUTANT",
           T_OWNER),
    Mutant("O3c 重做端点误走首次档(用户重做被按 390 扣)", GEO_API,
           "        feature_code=FEATURE_CODE_IMAGE_POST_REGEN,   # ← 便宜一档",
           "        feature_code=FEATURE_CODE_IMAGE_POST,  # MUTANT", T_OWNER),
    Mutant("O4 价目迁移丢幂等(重跑会炸)", PRICING_SQL,
           "ON CONFLICT (feature_code) DO UPDATE", "-- MUTANT no upsert", T_OWNER),
    Mutant("O5 门户泛化文案被删(Owner 点头挂的)", PORTAL,
           "AI 平台普遍存在个性化推荐,<b>不同用户实际看到的结果可能有所差异</b>。",
           "占位文案。  {/* MUTANT */}", T_OWNER),

    # ── §6b 模板方法论 ──
    Mutant("C1 GEO 默认骨架被改(不再逐实体)", CARD_TPL,
           'DEFAULT_SKELETON = "per_entity"',
           'DEFAULT_SKELETON = "pro_con"  # MUTANT', T_TPL),
    Mutant("C2 caveat 不再必填(纯夸卡混进来)", CONTENT_GEN,
           "        if not entity or not caveat:",
           "        if not entity:  # MUTANT", T_TPL),
    # C3 换过一版:原先只删 _BASE_STYLE 里的主色,但三个模板各自还写了一次,
    # invariant 并没被破 —— 那是无效变异,不是锁没判别力。
    # 现在打在【内容卡忽略传入的组风格】上,这才真的整组跑色。
    # 🔴 2026-08-03 锚点跟随实现:原锚 `n_points = max(2, min(len(points or []), 4))`
    #    随「要点文字必须进 prompt」那处修复改写了。锚点必须跟着走 ——
    #    指着旧写法的话 selftest 会当场报"锚点未命中"(它就是这么被抓到的),
    #    而不是安静地留下一个永远杀不掉的假绿变异。
    Mutant("C3 内容卡忽略组风格参数(整组跑色)", CARD_TPL,
           "    n_points = max(2, min(len(point_lines), 4)) if point_lines else 2",
           "    n_points = max(2, min(len(point_lines), 4)) if point_lines else 2; "
           "style = StyleTokens()  # MUTANT",
           T_TPL),
    Mutant("C4 顶部固定标题条不传(逐张各写各的)", CARD_TPL,
           '        f"文字为「{style.header_bar}」；正文区左上是本卡主标题「{headline[:24]}」；"',
           '        f"正文区左上是本卡主标题「{headline[:24]}」；"  # MUTANT', T_TPL),
    Mutant("C5 封面字数上限放飞(违背 §4 实证)", CARD_TPL,
           "COVER_TEXT_MAX = 30", "COVER_TEXT_MAX = 300  # MUTANT", T_TPL),
    Mutant("C6 生成器不再接客户素材", CONTENT_GEN,
           "    ctx = await build_brand_context(brand_id, kw, brand_name)",
           "    ctx = _NoCtx()  # MUTANT", T_TPL),
    Mutant("C7 未授权图片也进上下文(版权面失守)", KB_CTX,
           "                  AND COALESCE(publish_allowed, 0) = 1",
           "                  -- MUTANT", T_TPL),
    Mutant("C8 无素材时输出占位文案(诱导模型编)", KB_CTX,
           '        return "\\n".join(parts)',
           '        return "未提供"  # MUTANT', T_TPL),

    # ── §6c 标题长度分档(实测采纳率)──
    Mutant("L1 标题退回最差档(16-25字)", TITLE_ENGINE,
           '    _Template("{city}{kw}哪家好？{n}家实测对比：报价区间、工期和避坑要点一次说清",',
           '    _Template("{city}{kw}哪家好，{n}家对比分享",  # MUTANT', T_TITLE),
    Mutant("L2 长度下限被调到最差档内", TITLE_ENGINE,
           "TITLE_MIN_TARGET = 26", "TITLE_MIN_TARGET = 10  # MUTANT", T_TITLE),
    # 🔴 verify 套件要指向【真的断言它的那个文件】——
    #    原先写成 T_TPL,而断言其实在 T_TITLE 里,于是变异永远杀不掉(实测存活)。
    Mutant("L3 正文接力要求从 prompt 里删掉", CONTENT_GEN,
           "     让「标题 + 正文首句」合起来达到 60-100 字的信息量。",
           "     (MUTANT removed)", T_TITLE),

    # ── 自审抓到的张数预算缺陷(回归变异)──
    Mutant("D1 张数预算失效(want=1 又出 3 张)", IMAGE_PIPE,
           "    if max_cards and len(out) > int(max_cards):",
           "    if False:  # MUTANT", T_TPL),
    Mutant("D2 截断优先级反了(先砍封面)", IMAGE_PIPE,
           '        cover_part = [o for o in out if o["kind"] == "cover"][:budget]',
           '        cover_part = []  # MUTANT', T_TPL),

    # ── 详情页交互补全(工单「编码验收清单」九条)──
    # 🔴 本组锚点一律【单行】。多行锚点在这份 runner 里踩过一次:
    #    写入时的转义层数一多,\n 会变成真换行把 runner 自己写成语法错误。
    #    单行锚点没有这个问题,且 --selftest 能立刻验证是否命中。
    Mutant("U1 重抽额度改成先查后写(并发超发)", GEO_DB,
           "                WHERE id = %s AND deleted_at IS NULL AND redraw_count < %s",
           "                WHERE id = %s AND deleted_at IS NULL  -- MUTANT", T_UI),
    Mutant("U2 重抽上限从 10 放到 10000", REDRAW,
           "REDRAW_LIMIT_PER_POST = 10",
           "REDRAW_LIMIT_PER_POST = 10000  # MUTANT", T_UI),
    Mutant("U3 额度退回变成空操作(失败也扣次数)", GEO_DB,
           "                  SET redraw_count = GREATEST(redraw_count - 1, 0), updated_at = NOW()",
           "                  SET updated_at = NOW()  -- MUTANT", T_UI),
    Mutant("U4 单卡重抽整份重写 oss_keys(覆盖他卡)", GEO_DB,
           "                  SET oss_keys = jsonb_set(oss_keys, %s::text[], to_jsonb(%s::text), false),",
           "                  SET oss_keys = %s::jsonb,  -- MUTANT", T_UI),
    Mutant("U5 重抽下标越界防护被删", GEO_DB,
           "                  AND jsonb_array_length(oss_keys) > %s",
           "                  -- MUTANT", T_UI),
    Mutant("U6 联系方式开关不置「收尾卡待重抽」(假联动)", GEO_DB,
           "                      closing_stale = TRUE,",
           "                      -- MUTANT", T_UI),
    Mutant("U7 收尾卡 prompt 丢掉联系方式", CARD_TPL,
           '        + (f"再下方一行联系方式「{contact}」；" if contact else "")',
           '        + ""  # MUTANT', T_UI),
    Mutant("U8 频控不喂真值(日限规则恒不触发)", GEO_API,
           "                              per_account_today=per_account)",
           "                              )  # MUTANT", T_UI),
    Mutant("U9 今日已发把失败单也算进去", PUB_ADAPTER,
           "                  AND status NOT IN ('cancelled', 'withdrawn', 'failed', 'rejected')",
           "                  -- MUTANT", T_UI),
    Mutant("U10 账号搜索不转义 LIKE 元字符", PUB_ADAPTER,
           r'            .replace("%", "\\%")',
           r'            .replace("%", "%")  # MUTANT', T_UI),
    Mutant("U11 城市版本漏按创建人隔离(跨用户可见)", GEO_DB,
           "                  AND s.created_by = cur.created_by",
           "                  -- MUTANT", T_UI),
    Mutant("U12 没有比对基准也给黄点(假数据)", KB_CONS,
           "    if not report.checked:",
           "    if False:  # MUTANT", T_UI),
    Mutant("U13 结构性数字也标黄(黄点淹在噪声里)", KB_CONS,
           '    "元", "万", "亿", "年", "月", "天", "%", "折", "平",',
           '    "元", "万", "亿", "年", "月", "天", "%", "折", "平", "步", "家",  # MUTANT',
           T_UI),
    Mutant("U14 授权图 SQL 退回 boolean 写法(生产恒抛异常)", KB_CTX,
           "                  AND COALESCE(rights_confirmed, 0) = 1",
           "                  AND COALESCE(rights_confirmed, FALSE) = TRUE  -- MUTANT", T_UI),
    Mutant("U15 取素材失败不再留痕(退回静默降级)", KB_CTX,
           '        ctx.load_errors.append("brand_image_assets")',
           '        pass  # MUTANT', T_UI),
    Mutant("U16 缺授权图时不降级(编假实景图)", CARD_TPL,
           "    if preset.needs_photo and not has_authorized_photo:",
           "    if False:  # MUTANT", T_UI),
    Mutant("U17 价目端点兜一个默认价(显示与实扣不一致)", GEO_API,
           "            return None",
           "            return {'cost_points': 390}  # MUTANT", T_UI),
    Mutant("U18 详情页打开不回到封面(违背 P1)", DETAIL_TSX,
           "            if (!keepIdx) setActiveIdx(0);",
           "            // MUTANT", T_UI),
    Mutant("U19 手机预览改读服务端那份文案(两套文案)", DETAIL_TSX,
           "                                            {title || '（还没写标题）'}",
           "                                            {detail?.post.title || '（还没写标题）'}",
           T_UI),
    Mutant("U20 张序角标被删", DETAIL_TSX,
           "                                        {activeIdx + 1}/{total}",
           "                                        {/* MUTANT */}", T_UI),
    # 锚点跟随实现:互动栏 2026-08-03 抽成 `PhoneActionRail` 组件,className 变了。
    # 对应的锁也从"锁整串 className"改成"锁 PhoneActionRail 函数体里不许有数字"。
    Mutant("U21 互动位编造假数字", DETAIL_TSX,
           '<span className="text-[9px] leading-none text-white/75">—</span>',
           '<span className="text-[9px] leading-none text-white/75">1.2万</span>', T_UI),
    Mutant("U22 下载全部挪回顶栏", DETAIL_TSX,
           '                                    data-testid="download-all"',
           '                                    data-testid="download-all-moved"', T_UI),
    Mutant("U23 migration_020 漏登记(上线后字段不存在)", MANIFEST,
           '    "db/migration_020_geo_douyin_detail_ui_2026_08_02.sql",',
           '    # MUTANT', T_UI),
    Mutant("U24 城市 chip 不切作品(只换个名字)", DETAIL_TSX,
           "onNavigate?.(s.id)",
           "void 0  /* MUTANT */", T_UI),
    # ── 右栏知识库卡(锚点一律单行 —— 多行锚点在这份 runner 上踩过两次)──
    Mutant("U25 右栏计数不再要求「已确权」(与生成侧走岔)", KB_CTX,
           '                  AND COALESCE(rights_confirmed, 0) = 1"""',
           '                  AND TRUE"""  # MUTANT', T_UI),
    Mutant("U26 缩略图另起一套取法(不再优先 thumbnail_key)", KB_CTX,
           '    tk = (row or {}).get("thumbnail_key") or (row or {}).get("safe_size_key")',
           '    tk = None  # MUTANT', T_UI),
    Mutant("U27 「用到了」不再读生产留痕(拿库存冒充出处)", KB_CTX,
           "    return [SOURCE_LABELS.get(s, s) for s in raw]",
           "    return []  # MUTANT", T_UI),
    Mutant("U28 资料未知时显示 0 而不是 —(读成'客户没填')", DETAIL_TSX,
           ": '资料 —'}",
           ": '资料 0/10'}", T_UI),
    # 锚点跟随实现走:这段逻辑 2026-08-03 从端点搬进共享的
    # knowledge_context.build_client_knowledge(SSOT L6 知识库读取不得两套)。
    # 🔴 锚点跟随实现走(2026-08-03):build_client_knowledge 从 geo_douyin 的
    #    knowledge_context 搬到**跨板块共享**的 services/client_knowledge.py
    #    (写文章与做图文共用一份,原因是两边分母不一样:8 vs 10)。
    Mutant("U29 知识库卡取失败被当成没数据", CLIENT_KB,
           '        return {**empty, "has_brand": True, "load_failed": True}',
           '        return empty  # MUTANT', T_UI),
    Mutant("U30 资料填充度分母被改小(编一个假分母)", KB_CTX,
           '    ("team_size", "团队规模"),',
           '    # MUTANT', T_UI),
    Mutant("U31 生图异常诊断丢掉异常类型(空消息异常=零线索)", IMG_CLIENT,
           '        detail = f"{type(e).__name__}: {e}".rstrip(": ").strip()',
           '        detail = str(e)[:120]  # MUTANT', T_UI),

    # ── 样板图入库(复审 604269bc 抓到的唯一阻断项)──
    Mutant("U32 .gitignore 白名单被删(全局 *.jpg 又把样板图吞掉)", GITIGNORE,
           "!frontend/src/assets/style-samples/*.jpg",
           "# MUTANT", T_UI),
    # 🔴 2026-08-04 改打靶:样板图 import 随选择器搬到共用组件了。
    #    锚点留在 DETAIL_TSX 上会 replace 无效 —— 变异跑出来是"锚点未命中",
    #    看着像基础设施故障,实际是"这条锁已经不盯着任何东西了"。
    Mutant("U33 TSX 出现指向树里没有的图的 import(干净检出构建炸)", STYLE_PICKER,
           "import designTextSample from '@/assets/style-samples/design_text.jpg';",
           "import designTextSample from '@/assets/style-samples/design_text.jpg';\n"
           "import ghostSample from '@/assets/style-samples/not_in_tree.jpg';  // MUTANT",
           T_UI),
    # ── 生图链三跳韧性(返工单 REWORK_ORDER_IMAGE_RETRY_2026-08-02)──
    # 🔴 挑变异时避开会导致**死循环**的改法(例如让 poll 丢拍不计预算 +
    #    脚本永远抛错 = runner 挂死)。改用"预算算错 10 倍"这种会终止的等价破坏。
    Mutant("R1 submit 重试类混入 ReadTimeout(重复付费)", IMG_CLIENT,
           "    return (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)",
           "    return (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError, httpx.ReadTimeout)  # MUTANT",
           T_RETRY),
    Mutant("R2 无条件重试(一次成功也重发)", IMG_CLIENT,
           "            return await _submit(prompt, size, resolution, n, image_urls)",
           "            await _submit(prompt, size, resolution, n, image_urls)  # MUTANT",
           T_RETRY),
    Mutant("R3 poll 丢拍预算算错(提前放弃已计费任务)", IMG_CLIENT,
           "                waited += _POLL_INTERVAL_S\n                continue",
           "                waited += _POLL_INTERVAL_S * 10  # MUTANT\n                continue",
           T_RETRY),
    Mutant("R4 poll 不接 connect 异常(一抖就丢掉已计费任务)", IMG_CLIENT,
           "            except _connect_error_types() as exc:",
           "            except ZeroDivisionError as exc:  # MUTANT", T_RETRY),
    Mutant("R5 poll 里偷偷重新提交(重复付费)", IMG_CLIENT,
           "                misses += 1",
           "                misses += 1; await _submit('', '3:4', '1k', 1)  # MUTANT",
           T_RETRY),
    Mutant("R6 download 跳退回零重试", IMG_CLIENT,
           "    total = download_retries()",
           "    total = 1  # MUTANT", T_RETRY),
    Mutant("R7 download 重试绕过 SSRF 校验", IMG_CLIENT,
           "            _validate_download_url(current)",
           "            pass  # MUTANT", T_RETRY),
    Mutant("R8 代理标识打出凭据(user/pass 泄漏进日志与 error)", IMG_CLIENT,
           '        host = parsed.hostname or "?"',
           '        host = parsed.netloc or "?"  # MUTANT', T_RETRY),
    Mutant("R9 退避丢掉 jitter(5 连击落在同一抖动窗口)", IMG_CLIENT,
           "    return ceiling * (0.5 + random.random() * 0.5)",
           "    return ceiling  # MUTANT", T_RETRY),
    Mutant("R10 1k 成本回到未订正的 0.006", IMG_CLIENT,
           "IMAGE_COST_USD_1K = 0.01",
           "IMAGE_COST_USD_1K = 0.006  # MUTANT", T_RETRY),
    Mutant("R11 头注释没跟着常量改(下一个读者继续信错)", IMG_CLIENT,
           "cost ~$0.01/张(1k)",
           "cost ~$0.006/张", T_RETRY),
    Mutant("R12 重试次数写死不读 env(梯子变了调不动)", IMG_CLIENT,
           '    return _int_env("MARKETING_IMAGE_CONNECT_RETRIES", 5)',
           "    return 5  # MUTANT", T_RETRY),
    Mutant("R13 tracker 按张计价档失效(图片成本记≈0)", TRACKER,
           '                                 "flat_rate_per_call": APIMART_IMAGE_CALL_CNY},',
           '                                 "flat_rate_per_call": None},  # MUTANT', T_RETRY),
    # ── 封面质感升级(Owner 2026-08-02 · 对标 10 母版)──
    # C9 随裁定① 重构改指:背景图层已收进组级共享段(那条由 G12 专管),
    # 这里改测"封面自己没拿到共享段",与 G10(收尾)/G11(内容)凑齐三面。
    # 锚点跟随实现走:2026-08-03 在共享质感段与结构段之间插入了组内进度识别
    # (规范 §2 第 4 条的 i/N),锚点必须跟着变,否则永远命中不到 = 假的"全杀"。
    Mutant("C9 封面没拿到组级质感段", CARD_TPL,
           "        group_texture_block(style) +\n"
           "        series_progress_line(idx, total) +\n"
           "        # 以下是**封面特有的结构**(其他两类各有各的结构,不共享)",
           "        series_progress_line(idx, total) +  # MUTANT\n"
           "        # 以下是**封面特有的结构**(其他两类各有各的结构,不共享)", T_TPL),
    Mutant("C10 默认款退回扁平留白(这次升级等于没做)", CARD_TPL,
           '        style_def=("文字主导的深色编辑风信息图：近黑底，一层真实行业场景照片压暗作背景层；"',
           '        style_def=("扁平化信息图设计，大面积留白；"  # MUTANT', T_TPL),
    Mutant("C11 封面丢掉「标题逐字=查询词」硬律", CARD_TPL,
           '        + f"主标题必须逐字就是「{clip_text(title, COVER_TEXT_MAX)}」，不要改写成创意标题；"',
           '        + ""  # MUTANT', T_TPL),
    # 🔴 C12 换过一版:原先锚点删的是"压在窄色条上",而锁断言的是"三分之一" ——
    #    两者不是同一句,变异永远杀不掉(实测存活)。锚点必须打在**锁真正断言的东西**上。
    Mutant("C12 封面丢掉显式字号层级(层次被拉平)", CARD_TPL,
           '        + (f"标题正下方是副标题「{sub}」，字号约为主标题的三分之一，"',
           '        + (f"标题正下方是副标题「{sub}」，"  # MUTANT', T_TPL),
    # ── 组内风格一致性(Owner 2026-08-02 裁定①②)──
    Mutant("G10 组级质感段没进收尾卡(只富养封面复发)", CARD_TPL,
           "        group_texture_block(style) +\n"
           "        series_progress_line(idx, total) +\n"
           "        # 以下是收尾卡特有的结构",
           "        series_progress_line(idx, total) +  # MUTANT\n"
           "        # 以下是收尾卡特有的结构", T_TPL),
    Mutant("G11 组级质感段没进内容卡", CARD_TPL,
           "        group_texture_block(style) +\n"
           "        series_progress_line(card_index or idx, total) +\n"
           "        role_line + layout_line +\n"
           "        # 以下是内容卡特有的结构",
           "        series_progress_line(card_index or idx, total) +  # MUTANT\n"
           "        role_line + layout_line +\n"
           "        # 以下是内容卡特有的结构", T_TPL),
    Mutant("G12 共享质感段丢掉背景图层", CARD_TPL,
           '        f"照片压暗作背景层，文字层与照片层界限分明；"',
           '        f"整组统一质感："  # MUTANT', T_TPL),
    Mutant("G13 三类卡结构被写成同一份(共享过头)", CARD_TPL,
           'f"本张是这一组的收尾卡：画面中央是「{clip_text(headline, 20)}」，"',
           'f"本张是收尾卡。"  # MUTANT', T_TPL),

    # ── 垫图锚 + 禁抄段(第 0 步 A/B 裁定落地)──
    Mutant("G14 传参考图却不带禁抄段(渗漏 3/3 复发)", IMAGE_PIPE,
           "        res.prompt = with_reference_guard(res.prompt)",
           "        pass  # MUTANT", T_IMAGE),
    Mutant("G15 封面锚不传给其余卡(组内风格锁失效)", IMAGE_PIPE,
           "        *(_one(i, s, anchor or None) for i, s in rest))",
           "        *(_one(i, s, None) for i, s in rest))  # MUTANT", T_IMAGE),
    Mutant("G16 封面自己也被垫图(自己锚自己)", IMAGE_PIPE,
           "    cover_res = await _one(cover_idx, items[cover_idx])",
           "    cover_res = await _one(cover_idx, items[cover_idx], ['x'])  # MUTANT",
           T_IMAGE),
    Mutant("G17 两阶段打乱卡序(落库 oss_keys 与 cards 对不上)", IMAGE_PIPE,
           "    results = [by_index[i] for i in range(len(items))]",
           "    results = [cover_res] + list(rest_results)  # MUTANT", T_IMAGE),

    # ── 轮询策略(按 provider 真字段重定)──
    Mutant("G18 轮询上限退回 150s(慢档 33% 直接判死)", IMG_CLIENT,
           "_POLL_MAX_WAIT_S = 420.0", "_POLL_MAX_WAIT_S = 150.0  # MUTANT", T_RETRY),
    Mutant("G19 首轮不按 estimated_time 延迟(白烧 RPM 额度)", IMG_CLIENT,
           "    return max(_POLL_FIRST_DELAY_MIN_S,\n"
           "               min(est * _POLL_FIRST_DELAY_RATIO, _POLL_FIRST_DELAY_MAX_S))",
           "    return _POLL_FIRST_DELAY_MIN_S  # MUTANT", T_RETRY),
    Mutant("G20 真进度不透出(前端只能靠模拟)", IMG_CLIENT,
           "        if on_progress is None:\n            return",
           "        return  # MUTANT", T_RETRY),
    Mutant("G21 进度回调异常拖垮已计费生图", IMG_CLIENT,
           "        except Exception as e:  # noqa: BLE001 - 进度回调炸了不该拖垮生图\n"
           "            logger.warning(\"[image] 进度回调异常(已忽略): %s\",\n"
           "                           f\"{type(e).__name__}: {e}\".rstrip(\": \"))",
           "        except ZeroDivisionError:  # MUTANT\n            pass", T_RETRY),

    # ── 异步化(Owner 2026-08-02「异步化和入口页一起做」)──
    # 前提事实:生产 nginx 对 /api/geo-douyin/* 走默认 location,60s 超时;
    # 一组卡实测 ≈137s → 同步必然 504。下面每个变异都在还原一种"看起来还行、
    # 实际上生产跑不通/进度是假的"的写法。
    Mutant("Y1 下单端点退回同步等生产(=生产必然 504)", GEO_API,
           "    dispatch_production(\n"
           "        post_id=post_id, user_id=user_id, keyword=req.keyword.strip(),",
           "    await run_image_post_production(  # MUTANT\n"
           "        post_id=post_id, user_id=user_id, keyword=req.keyword.strip(),",
           T_ASYNC),
    Mutant("Y2 重新创作退回同步等(整组 137s 同样必炸)", GEO_API,
           "    dispatch_production(\n"
           "        post_id=post_id, user_id=int(user[\"user_id\"]),",
           "    await run_image_post_production(  # MUTANT\n"
           "        post_id=post_id, user_id=int(user[\"user_id\"]),",
           T_ASYNC),
    # 锚点跟随实现走:2026-08-03 §15 给 dispatch_redraw 加了结算回调,
    # 原锚点(无 on_settled)已失效 —— selftest 当场报红,这正是它该有的样子。
    Mutant("Y3 重抽退回同步(前端报失败但图已出、额度已扣)", GEO_API,
           "    task_id, _ = await dispatch_redraw(post, int(card_index), hint=req.hint,\n"
           "                                       user_id=user_id,\n"
           "                                       freeze_id=redraw_freeze_id,\n"
           "                                       on_settled=_settle, on_failed=_refund)",
           "    task_id = 0; await redraw_one_card(post, int(card_index))  # MUTANT",
           T_ASYNC),
    Mutant("Y4 额度用满改到后台再判(用户要等轮询才知道)", GEO_API,
           "    if used_now >= REDRAW_LIMIT_PER_POST:",
           "    if False:  # MUTANT", T_ASYNC),
    Mutant("Y5 后台任务不留强引用(事件循环可能 GC 掉,偶发跑一半没了)", PROD_TASK,
           "    task = asyncio.create_task(_runner())\n"
           "    _RUNNING_PRODUCTIONS.add(task)\n"
           "    task.add_done_callback(_RUNNING_PRODUCTIONS.discard)",
           "    task = asyncio.create_task(_runner())  # MUTANT", T_ASYNC),
    Mutant("Y5b 重抽后台任务不留强引用(同一 GC 坑,曾漏测)", REDRAW,
           "    task = asyncio.create_task(_runner())\n"
           "    _RUNNING_REDRAWS.add(task)\n"
           "    task.add_done_callback(_RUNNING_REDRAWS.discard)",
           "    task = asyncio.create_task(_runner())  # MUTANT", T_ASYNC),
    Mutant("Y6 强引用集合降为函数内局部变量(等于没有强引用)", PROD_TASK,
           "_RUNNING_PRODUCTIONS: set = set()",
           "_UNUSED_RUNNING: set = set()  # MUTANT", T_ASYNC),
    Mutant("Y7 任务行退回冻结之后建(余额不足查不到→永远转圈)", PROD_TASK,
           "    task_id = await asyncio.to_thread(\n"
           "        ddb.create_task, post_id=post_id, user_id=user_id,\n"
           "        task_ref=task_ref, freeze_id=None, progress_total=want,\n"
           "    )\n    outcome.task_id = task_id",
           "    task_id = 0  # MUTANT", T_ASYNC),
    Mutant("Y8 冻结失败不落任务行/作品态(前端永远转圈)", PROD_TASK,
           "        await asyncio.to_thread(\n"
           "            ddb.update_task, task_id, status=\"failed\", stage=\"freeze\",\n"
           "            error_msg=outcome.error, mark_finished=True,\n"
           "        )\n"
           "        await asyncio.to_thread(ddb.set_post_status, post_id, \"failed\")",
           "        pass  # MUTANT", T_ASYNC),
    Mutant("Y9 进度自增退回读改写(并发 8 会丢计数且不报错)", GEO_DB,
           "                  SET progress_done = COALESCE(progress_done, 0) + 1,",
           "                  SET progress_done = (SELECT 1),  # MUTANT", T_ASYNC),
    Mutant("Y10 列表少选时间列(卡住判定永远判不出→永远转圈)", GEO_DB,
           "                      started_at, finished_at, created_at, updated_at\n"
           "                 FROM geo_douyin_post_tasks\n"
           "                WHERE post_id = ANY(%s::bigint[])",
           "                      started_at, finished_at\n"
           "                 FROM geo_douyin_post_tasks  -- MUTANT\n"
           "                WHERE post_id = ANY(%s::bigint[])", T_ASYNC),
    Mutant("Y11 逐张进度回调不接(进度只能 0 跳 100=假进度条)", IMAGE_PIPE_2,
           "        await _tick()\n        return res",
           "        return res  # MUTANT", T_ASYNC),
    Mutant("Y12 回调返回的协程不 await(async 回调形同虚设)", IMAGE_PIPE_2,
           "            if inspect.isawaitable(r):\n                await r",
           "            pass  # MUTANT", T_ASYNC),
    Mutant("Y13 进度回调异常拖垮整组已出的图", IMAGE_PIPE_2,
           "        except Exception as e:  # noqa: BLE001 - 记进度失败不该让已出的图作废\n"
           "            logger.warning(\"[douyin-image] 进度回调失败(不影响出图): %s\",\n"
           "                           f\"{type(e).__name__}: {e}\".rstrip(\": \"))",
           "        except ZeroDivisionError:  # MUTANT\n            pass", T_ASYNC),
    Mutant("Y14 终态任务也按「多久没动」判卡住(历史任务全被判成中断)", TASK_PROG,
           "    stalled = (raw_state not in TERMINAL_STATES\n"
           "               and idle >= TASK_STALE_SECONDS)",
           "    stalled = idle >= TASK_STALE_SECONDS  # MUTANT", T_ASYNC),
    Mutant("Y15 卡住之后继续轮询(永远转圈)", TASK_PROG,
           '        "active": state in ("queued", "running"),',
           '        "active": state != "succeeded",  # MUTANT', T_ASYNC),
    Mutant("Y16 百分比不随张数推进(进度条是死的)", TASK_PROG,
           "    if key == \"images\" and total > 0:\n"
           "        frac = max(0.0, min(1.0, float(done) / float(total)))\n"
           "        return int(round(lo + (hi - lo) * frac))",
           "    if False:  # MUTANT\n        pass", T_ASYNC),
    Mutant("Y17 估不出来时瞎编一个数(而不是不显示)", TASK_PROG,
           "    if total <= 0:\n        return None",
           "    if total <= 0:\n        return 60  # MUTANT", T_ASYNC),
    Mutant("Y18 阶段名直接吐英文(违背「术语翻人话」)", TASK_PROG,
           '    "images": "正在画卡片图",', '    "images": "images",  # MUTANT', T_ASYNC),

    # ── 创建入口页补课(补课单 3 四条)──
    Mutant("E1 试点横幅退回常驻(不是压一行+展开)", ENTRY_TSX,
           "                {bannerOpen && (",
           "                {true && (  /* MUTANT */", T_ASYNC),
    Mutant("E2 状态徽章文案写两处(改一处另一处还在)", ENTRY_TSX,
           "    ready: { label: '待发布', className: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' },",
           "    ready: { label: '待发布', className: 'x' }, // MUTANT\n"
           "    readyDup: { label: '待发布', className: 'y' },", T_ASYNC),
    Mutant("E3 失败条目不给人话原因(退回只写「没做成」)", ENTRY_TSX,
           "                                                    {p.failure_reason || '这次没做成，费用已自动退回'}",
           "                                                    {'没做成'}{/* MUTANT */}", T_ASYNC),
    # 锚点跟随实现走:显示的从"基础价"改成"这个张数下的实付价"(加张计价上线后,
    # 显示基础价会让选 9 张的用户看到 4 张的价 —— 那是最坏的一种"价格可见")。
    Mutant("E4 价格写死在前端(违背价目表 SSOT)", ENTRY_TSX,
           "                            {batchPrice !== null && ` · 合计 ${batchPrice} 算力`}",
           "                            {` · 合计 390 算力`}  {/* MUTANT */}", T_ASYNC),
    Mutant("E5 入口页给每条内容各起一个轮询(5 城 = 5 条常驻请求)", ENTRY_TSX,
           "    const anyActive = useMemo(",
           "    const _x = useDouyinPostTask(null); const anyActive = useMemo(  // MUTANT",
           T_ASYNC),
    Mutant("E6 前端自算进度(后端说 40% 前端显示 60%)", ENTRY_TSX,
           "                                                        style={{ width: `${Math.max(2, prog.percent)}%` }}",
           "                                                        style={{ width: `${Date.now() % 100}%` }} /* MUTANT */",
           T_ASYNC),
    Mutant("E7 hook 不认后端 active,自己判跑完", TASK_HOOK,
           "            if (!next.active) {\n                stop();",
           "            if (false) {  // MUTANT\n                stop();", T_ASYNC),
    Mutant("E8 估不出来也硬显示一个时间", TASK_HOOK,
           "    if (seconds === null || seconds === undefined) return '';",
           "    if (seconds === null || seconds === undefined) return '约 1 分钟';  // MUTANT",
           T_ASYNC),

    # ── 收尾三件(2026-08-03)──
    # 🔴 必须**两条一起撤**。表里有 `/api/geo-douyin/` 和无斜杠的 `/api/geo-douyin`
    #    两条,而 resolve_permission 用 startswith —— 只删一条另一条照样命中,
    #    那个变异是 no-op(实测存活过一次)。变异要还原的是"归属真的没了"这个状态,
    #    不是"少了一行"。同型坑:G17 样本没判别力。
    Mutant("R1 撤掉 geo-douyin 的 RBAC 归属(=真实事故:上线后只有 admin 能用)",
           MODULE_MAP,
           '    ("/api/geo-douyin/",    "writing"),       # AI 创作中心 · GEO 图文制作\n'
           '    ("/api/geo-douyin",     "writing"),       # 兼容无斜杠',
           "    # MUTANT 两条归属全撤", T_FOLLOW),
    Mutant("R2 未映射改成默认放行(全站鉴权外门失效)", MODULE_MAP,
           '    return "__unmapped__"  # 默认拒绝',
           "    return None  # MUTANT", T_FOLLOW),
    Mutant("R3 n 不再夹到 provider 上限(按 n 张计费只用 1 张)", IMG_CLIENT,
           "    safe = max(1, min(want, _PROVIDER_MAX_N))",
           "    safe = max(1, min(want, 10))  # MUTANT", T_FOLLOW),
    Mutant("R4 夹了不留痕(静默改用户入参)", IMG_CLIENT,
           "    if want != safe:", "    if False:  # MUTANT", T_FOLLOW),
    Mutant("R5 2k 成本退回未核实旧值", IMG_CLIENT,
           '    "2k": 0.014,   # 定价中心 0.14 Credits/张',
           '    "2k": 0.012,  # MUTANT', T_FOLLOW),
    Mutant("R6 4k 成本退回未核实旧值", IMG_CLIENT,
           '    "4k": 0.021,   # 定价中心 0.21 Credits/张',
           '    "4k": 0.024,  # MUTANT', T_FOLLOW),

    # ── §15 部分成功交付(Review 点名的四对 + 边界)──
    Mutant("S1 封面失败也当可交付(变异对①:封面失败必全退)", SETTLE,
           "    if not card_is_ok(cover):\n        return False",
           "    if False:  # MUTANT\n        return False", T_PARTIAL),
    Mutant("S2 门槛降到「有一张就算」(等于没门槛)", SETTLE,
           "    return sum(1 for c in items if card_is_ok(c)) >= min_ok_required(len(items))\n"
           "\n\ndef pending_indices",
           "    return True  # MUTANT\n\n\ndef pending_indices", T_PARTIAL),
    Mutant("S3 门槛取整取反(5 张只要 2 张就交付)", SETTLE,
           "    return max(1, math.ceil(int(total) / 2))",
           "    return max(1, int(total) // 2)  # MUTANT", T_PARTIAL),
    Mutant("S4 没齐也 commit(变异对②:补齐中不得 commit)", SETTLE,
           "    if pending_indices(cards):\n        return \"not_complete\"",
           "    if False:  # MUTANT\n        return \"not_complete\"", T_PARTIAL),
    Mutant("S5 抢占退化成先查后改(变异对③:并发下 commit 两次)", GEO_DB,
           "                WHERE id = %s AND status = 'completing'",
           "                WHERE id = %s  -- MUTANT", T_PARTIAL),
    Mutant("S6 抢不到也照样 commit(变异对③ 的另一半)", SETTLE,
           "    if not claimed:\n        return \"already\"",
           "    claimed = claimed or {}  # MUTANT\n    if False:\n        return \"already\"",
           T_PARTIAL),
    Mutant("S7 commit 失败回滚抢占(反而制造重复扣款)", SETTLE,
           "            logger.error(\"[douyin-settle] commit 失败 task_ref=%s freeze_id=%s: %s\",\n"
           "                         task_ref, freeze_id, e)",
           "            raise  # MUTANT", T_PARTIAL),
    Mutant("S8 自动补齐改走用户额度(变异对④)", PROD_TASK,
           "                res = await render_one_card(\n"
           "                    post_id, i + 1, str(spec.get(\"headline\") or \"\"), \"\",\n"
           "                    prompt_override=str(spec.get(\"prompt\") or \"\"))",
           "                from services.geo_douyin.redraw import redraw_one_card  # MUTANT\n"
           "                res = await redraw_one_card({\"id\": post_id}, i)", T_PARTIAL),
    Mutant("S9 自动补齐轮数上限改 0(等于不补)", SETTLE,
           "AUTO_FILL_MAX_ROUNDS = 2", "AUTO_FILL_MAX_ROUNDS = 0  # MUTANT", T_PARTIAL),
    # 🔴 这条变异改过两次,两次都是我自己的问题,记下来:
    #    ① 一开始挂 T_PARTIAL —— 能抓它的行为锁在 billing 套件里,挂错了套件;
    #    ② 改挂 T_BILL 后仍存活,真因是**样本没判别力**:原变异改的是取值
    #       (`res.oss_key if res else "x"`),而 fixture 里失败卡的 oss_key
    #       本来就是空串 → 改完还是空串 = **no-op**(G17 / R1 同型第三次)。
    #    要还原的风险是「**把槽位丢掉**」,所以变异必须去掉那个空槽。
    Mutant("S10 失败卡不留占位(下标整体前移→重抽抽错张)", PROD_TASK,
           "            keys = [str(r.get(\"oss_key\") or \"\") for r in rows]",
           "            keys = [k for k in (str(r.get(\"oss_key\") or \"\") for r in rows) if k]  # MUTANT",
           T_BILL),
    Mutant("S11 补齐中被判成卡住(UI 误报中断)", TASK_PROG,
           "        state = STATE_COMPLETING\n        stalled = False",
           "        state = STATE_COMPLETING  # MUTANT", T_PARTIAL),
    Mutant("S12 重抽模块直接引入结算(破掉「重抽零计费」)", REDRAW,
           "import inspect\nimport logging",
           "import inspect\nimport logging\nfrom middleware import billing  # MUTANT",
           T_PARTIAL),

    # ══════════════════════════════════════════════════════════
    # 2026-08-03 全量包:客户作用域 / 知识库同框 / 组图规范 / 按张计价
    # ══════════════════════════════════════════════════════════
    # ── 议题①:客户作用域统一 ──
    Mutant("W1 大厅退回后端不认的 brand_name 过滤(等于没过滤)", HALL_TSX,
           "`brand_id=${currentBrandId}`",
           "`brand_name=${currentBrandId}`  // MUTANT", T_V2),
    Mutant("W2 大厅根本不带客户参数(左上角选谁都一样)", HALL_TSX,
           "if (currentBrandId !== null && status !== 'optimizing') {",
           "if (false && currentBrandId !== null && status !== 'optimizing') {  // MUTANT",
           T_V2),
    Mutant("W3 后端把视角过滤当权限用(拿掉 RBAC)", SERVER_PY,
           "        allowed_brands = get_user_brand_filter(request)\n"
           "        projects = get_writing_projects(status, allowed_brands, brand_id=brand_id)",
           "        projects = get_writing_projects(status, None, brand_id=brand_id)  # MUTANT",
           T_V2),
    # 🔴 挂错套件的实例(自审记一笔):这条原来挂 T_V2,而能抓它的锁
    #    `test_workspace_gates_on_client_and_writes_back_to_global` 在 T_PIPE1 里。
    #    存活原因是"验证套件里根本没有那条锁",不是判据没判别力 —— 两种存活
    #    要分清,否则会去改一条本来就对的锁。
    Mutant("W4 创作中心不回写全局(变成第二个客户选择器)", WORKSPACE_TSX,
           "onClick={() => switchClient(c.id)}",
           "onClick={() => void c.id}  // MUTANT", T_PIPE1),
    Mutant("W5 入口页又自己拉一份客户列表(割裂复活)", ENTRY_TSX,
           "    const { currentBrandId, clientContext } = useClientContext();",
           "    const { currentBrandId, clientContext } = useClientContext();\n"
           "    void '/api/my-clients';  // MUTANT", T_PIPE1),

    # ── 议题③:知识库同框 ──
    Mutant("K1 分数改回自己数字段(第三份分母)", CLIENT_KB,
           '        "filled": int(summary.get("filled_count") or 0),\n'
           '        "total": int(summary.get("total_fields") or 0),',
           '        "filled": int(summary.get("filled_count") or 0),\n'
           '        "total": len(M3_FIELD_LABELS),  # MUTANT', T_V2),
    Mutant("K2 取素材列名改回手写 6 列(全量物料又丢四项)", KB_CTX,
           '    cols = ", ".join(k for k, _ in MATERIAL_FIELDS)\n'
           "    conn = get_connection()\n"
           "    try:\n"
           "        cur = conn.cursor()\n"
           "        cur.execute(\n"
           '            f"""SELECT {cols}',
           '    cols = "company_intro, core_selling_points, unique_value, case_studies, credentials, testimonials"  # MUTANT\n'
           "    conn = get_connection()\n"
           "    try:\n"
           "        cur = conn.cursor()\n"
           "        cur.execute(\n"
           '            f"""SELECT {cols}', T_V2),
    Mutant("K3 方法论/价格档位不进 prompt(填了也没用)", KB_CTX,
           '        if self.methodology:\n            parts.append(f"【方法论】{self.methodology}")',
           "        if False and self.methodology:  # MUTANT\n"
           '            parts.append(f"【方法论】{self.methodology}")', T_V2),
    Mutant("K4 入口页不再用共用知识库卡(又各画各的)", ENTRY_TSX,
           "                <ClientKnowledgeCard",
           "                <div data-was=\"ClientKnowledgeCar\" hidden  {...({} as any)}  // MUTANT\n"
           "                    ", T_V2),

    # ── 规范 §8:张数三层控制 / caveat / 冻结文案 / 串味 ──
    Mutant("G1 张数校验改成「有几张算几张」(静默凑数)", CONTENT_GEN,
           "    if not body or len(cards) != content_count:",
           "    if not body or not cards:  # MUTANT", T_V2),
    Mutant("G2 降档表砍掉收口(有头无尾)", SERIES_PLAN,
           '    4: ["cover", "compare", "cost", "closing"],',
           '    4: ["cover", "compare", "cost", "verify"],  # MUTANT', T_V2),
    Mutant("G3 收口卡 caveat 不进 prompt", CARD_TPL,
           '        + (f"小结下面单独一行小字提醒「{caveat_text}」；" if caveat_text else "")',
           '        + ""  # MUTANT', T_V2),
    Mutant("G4 封面导航词交回模型自加(逐字一致恒 fail)", CARD_TPL,
           '    aux_line = (f"底部一行小号导航词，逐字是「{\' / \'.join(labels)}」，"\n'
           '                f"不要增删任何一个词；" if labels else "")',
           '    aux_line = ""  # MUTANT', T_V2),
    Mutant("G5 串味词表一刀切(误伤本行业客户)", SERIES_PLAN,
           "        if industry == owner_industry:\n            continue",
           "        if False:  # MUTANT\n            continue", T_V2),
    Mutant("G6 版式不进生图 prompt(七张各说各话)", CARD_TPL,
           '    layout_line = (f"本张的信息构图必须是「{layout_role}」，"\n'
           '                   f"不要复制上一张的版式；" if layout_role else "")',
           '    layout_line = ""  # MUTANT', T_V2),

    # ── 议题②:选题蒸馏器 ──
    Mutant("D1 few-shot 不再优先同行业图文帖", CORPUS_DB,
           '        primary = _select("industry_key = %s AND is_image_post = TRUE", (industry,), n)',
           '        primary = _select("industry_key = %s AND is_image_post = FALSE", (industry,), n)  # MUTANT',
           T_V2),
    Mutant("D2 降级不回报(悄悄用别的样本)", CORPUS_DB,
           '    if not primary:\n        return got, "no_same_industry_image_post"',
           '    if not primary:\n        return got, ""  # MUTANT', T_V2),
    Mutant("D3 蒸馏 prompt 去掉禁抄段(串味渗漏)", DISTILL,
           "{samples_block}\n\n{NO_COPY_BLOCK}",
           "{samples_block}\n\n{''}  # MUTANT", T_V2),
    Mutant("D4 串味检测放行(抄了也不拦)", DISTILL,
           "        hits = detect_contamination(blob, sample_texts, allow=allow)\n"
           "        hits += detect_sample_taint(blob, industry_key)",
           "        hits = []  # MUTANT", T_V2),

    # ── 议题④:按张数计价 ──
    Mutant("P1 少于套餐张数变成负数抵扣(倒找钱)", PRICING,
           "    return max(0, clamp_card_count(card_count) - CARD_COUNT_INCLUDED)",
           "    return clamp_card_count(card_count) - CARD_COUNT_INCLUDED  # MUTANT", T_V2),
    Mutant("P2 价目读不到按 0 放行(多做的卡白送)", PRICING,
           "    unit = await read_unit_points(FEATURE_CODE_IMAGE_POST_EXTRA_CARD)\n"
           "    return n * unit",
           "    try:\n"
           "        unit = await read_unit_points(FEATURE_CODE_IMAGE_POST_EXTRA_CARD)\n"
           "    except PricingUnavailable:\n"
           "        return 0  # MUTANT\n"
           "    return n * unit", T_V2),
    Mutant("P3 加张费不进冻结(按 4 张收钱做 9 张)", PROD_TASK,
           "            extra_cost=extra_cost,",
           "            extra_cost=0,  # MUTANT", T_V2),
    Mutant("P4 自动补齐改走收费重抽链(让用户为系统的活付钱)", PROD_TASK,
           "        res = await render_one_card(",
           "        from services.geo_douyin.redraw import redraw_one_card  # MUTANT\n"
           "        res = await render_one_card(", T_V2),
    Mutant("P5 重抽失败不退款(每次失败漏一笔冻结)", GEO_API,
           "                                       on_settled=_settle, on_failed=_refund)",
           "                                       on_settled=_settle)  # MUTANT", T_UI),

    # ════════════════════════════════════════════════════════════
    # 2026-08-03 收尾包(v3):蒸馏定价 / 对齐核验 / OCR / 画幅
    # ════════════════════════════════════════════════════════════
    # ── V1 蒸馏定价 130 ──
    # 🔴 2026-08-05 蒸馏改异步:扣费上下文从端点搬进 run_distill_task,锚点跟着搬。
    Mutant("V1a 失败分支挪出扣费上下文(没蒸出来照样扣 130)", DISTILL_TASK,
           "            if not result.ok:",
           "            result_ok_mut = result.ok  # MUTANT\n"
           "            if False:", T_V3),
    # 🔴 2026-08-05 改异步:扣费上下文搬进后台任务,锚点跟着搬(不改则这条锁不盯任何东西)。
    Mutant("V1b 蒸馏整个不扣费(改回免费)", DISTILL_TASK,
           "        async with charge_on_success(int(user_id), feature_code, brand_id=int(brand_id)):",
           "        if True:  # MUTANT", T_V3),
    # 🔴 2026-08-05 改异步:占位函数搬进 distill_task.try_acquire_inflight,锚点跟着搬。
    Mutant("V1c 节流判定与占位之间插一个 await(并发下扣两次)", DISTILL_TASK,
           "    _INFLIGHT_BRANDS[int(brand_id)] = now\n    return True",
           "    import asyncio  # MUTANT\n"
           "    await asyncio.sleep(0)\n"
           "    _INFLIGHT_BRANDS[int(brand_id)] = now\n    return True", T_V3),
    # 🔴 工单点名要求的那条(WO-DISTILL-TIMEOUT-ASYNC-2026-08-05 §6):
    #    把异步改回同步 → 用例必须转红。这是本次改造的**全部意义**所在 ——
    #    同步版生产成功率 0/5(真实耗时 58.0s > nginx 60s 硬墙)。
    #    没有这条,"改成异步了"这件事本身没有任何断言守着。
    Mutant("V1f 提交路径改回同步等 LLM(退回 0/5 那个状态)", GEO_API,
           "        dispatch_distill(",
           "        from services.geo_douyin.topic_distiller import distill_topics  # MUTANT\n"
           "        await distill_topics(brand_id=brand_id, keywords=keywords,\n"
           "                             brand_name=brand_name, city='', industry_key='general', want=5)\n"
           "        dispatch_distill(", T_V3),
    Mutant("V1d 价格写死进前端(绕开价目表 SSOT)", ENTRY_TSX,
           "                            {!distilling && distillPrice !== null && (",
           "                            {!distilling && (distillPrice ?? 130) !== null && (  // MUTANT", T_V3),
    Mutant("V1e 蒸馏模块自己扣费(资金动作散开)", DISTILL,
           "    n = max(TOPIC_COUNT_MIN, min(int(want or TOPIC_COUNT_DEFAULT), TOPIC_COUNT_MAX))",
           "    from middleware.billing import charge_on_success  # MUTANT\n"
           "    n = max(TOPIC_COUNT_MIN, min(int(want or TOPIC_COUNT_DEFAULT), TOPIC_COUNT_MAX))",
           T_V3),

    # ── V2 §8.6-10 卡面与文案对齐 ──
    Mutant("V2a 对齐核验恒 ok(核了等于没核)", KB_CONS,
           "            report.issues.append(AlignmentIssue(\n"
           "                kind=\"number_conflict\", where=where,",
           "            continue  # MUTANT\n"
           "            report.issues.append(AlignmentIssue(\n"
           "                kind=\"number_conflict\", where=where,", T_V3),
    Mutant("V2b 改成「缺失即违规」(方法论计数被大面积误判)", KB_CONS,
           "            cap_values = cap_units.get(unit)\n"
           "            if not cap_values:\n"
           "                continue          # 文案里没提这个单位 → 不是矛盾,是分工(§8.5)",
           "            cap_values = cap_units.get(unit) or set()  # MUTANT", T_V3),
    Mutant("V2c 没有文案时报成「核过没问题」(前端会显示绿灯)", KB_CONS,
           "        return AlignmentReport(checked=False, reason=\"这条内容还没有文案，无法比对\")",
           "        return AlignmentReport(checked=True)  # MUTANT", T_V3),
    Mutant("V2d 详情页不看 checked 就渲染(把没核的显示成核过了)", DETAIL_TSX,
           "                            {consistency?.alignment?.checked\n"
           "                                && consistency.alignment.issues.length > 0 && (",
           "                            {consistency?.alignment\n"
           "                                && consistency.alignment.issues.length >= 0 && (  // MUTANT",
           T_V3),

    # ── V3 §8.6-11 OCR 逐字核验 + 要点文字进 prompt ──
    Mutant("V3a 内容卡退回「只用条数不用要点文字」(=本批修的那个真缺陷)", CARD_TPL,
           "    points_line = (\n"
           "        \"正文要点逐字排版如下，一条一行，不要改写、不要增删、不要自己补充：\"\n"
           "        + \"\".join(f\"「{t}」\" for t in point_lines) + \"；\"\n"
           "    ) if point_lines else f\"下方是 {n_points} 条要点，每条一行；\"",
           "    points_line = f\"下方是 {n_points} 条要点，每条一行；\"  # MUTANT", T_V3),
    Mutant("V3b 冻结文案漏掉要点(OCR 核验安静地少查一项)", CARD_TPL,
           '        out.extend([clip_text(x, 60) for x in (p.get("points") or [])\n                    if str(x).strip()][:4])',
           '        pass  # MUTANT', T_V3),
    Mutant("V3c 冻结文案卡序错位(拿第 2 张的答案核第 3 张的图)", OCR_QA,
           "    out: Dict[int, List[str]] = {}\n    idx = 0\n"
           "    if str(cover.get(\"title\") or \"\").strip():",
           "    out: Dict[int, List[str]] = {}\n    idx = 1  # MUTANT\n"
           "    if str(cover.get(\"title\") or \"\").strip():", T_V3),
    Mutant("V3d 视觉服务挂了报成「全都通过」", OCR_QA,
           "        if text is None:\n"
           "            return CardOcrCheck(card_index=i, checked=False,\n"
           "                                reason=\"文字识别服务暂时不可用\")",
           "        if text is None:\n"
           "            return CardOcrCheck(card_index=i, checked=True, ok=True)  # MUTANT",
           T_V3),
    Mutant("V3e OCR 端点开始收费(让用户为我方 QA 付钱)", GEO_API,
           "    report = await run_post_ocr_qa(post)\n"
           "    return {\"status\": \"success\", **report.to_dict()}",
           "    from middleware.billing import freeze_points  # MUTANT\n"
           "    report = await run_post_ocr_qa(post)\n"
           "    return {\"status\": \"success\", **report.to_dict()}", T_V3),
    Mutant("V3f OCR 去掉节流(可以无限打视觉服务)", GEO_API,
           "    _OCR_LAST_AT[post_id] = now",
           "    pass  # MUTANT", T_V3),

    # ── V4 §8.8 画幅自选 ──
    Mutant("V4a 白名单不收敛(非法画幅透传给 provider)", GEO_CONFIG,
           "    return key if key in ASPECT_RATIOS else ASPECT_RATIO_DEFAULT",
           "    return key or ASPECT_RATIO_DEFAULT  # MUTANT", T_V3),
    # 锚点跟随实现:`output_spec` 在把 OUTPUT_SPEC 重新变成承重常量时改写过。
    Mutant("V4b prompt 的输出规格不跟画幅走(文字排到画面外)", CARD_TPL,
           "    key = normalize_aspect_ratio(aspect_ratio)\n"
           "    if key == ASPECT_RATIO_DEFAULT:\n"
           "        return OUTPUT_SPEC\n"
           "    return ASPECT_RATIOS[key][\"spec\"]",
           "    return OUTPUT_SPEC  # MUTANT", T_V3),
    Mutant("V4c 给 provider 的画幅写死(选了 9:16 还是出 3:4)", IMAGE_PIPE,
           "    return ASPECT_RATIOS[normalize_aspect_ratio(aspect_ratio)][\"size\"]",
           "    return \"3:4\"  # MUTANT", T_V3),
    Mutant("V4d 整组出图漏传画幅(封面按选的出、其余按默认出)", IMAGE_PIPE,
           "                aspect_ratio=aspect_ratio,\n"
           "            )",
           "            )  # MUTANT", T_V3),
    Mutant("V4e 重抽不跟这条内容的画幅(一组里混两种画幅)", REDRAW,
           "    aspect_ratio = str(post.get(\"aspect_ratio\") or \"\")",
           "    aspect_ratio = \"\"  # MUTANT", T_V3),
    Mutant("V4f 迁移 024 漏登记(上线详情页直接打不开)", MANIFEST,
           '    "db/migration_024_geo_douyin_aspect_ratio_2026_08_03.sql",',
           "    # MUTANT", T_V3),
    # 🔴 V4g 的验证套件是 T_UI 不是 T_V3 —— 这条锁(`"3:4" in OUTPUT_SPEC`)
    #    一直都在,在 detail_ui 那个文件里。第一版我把它挂到 T_V3 上,变异存活,
    #    但那**不是"判据太松",是我指错了文件**。两种存活的处理完全不同:
    #    判据松要改锁,挂错套件要改 runner —— 混起来会去改一条本来就对的锁。
    Mutant("V4g 默认画幅的输出规格常量被改掉", CARD_TPL,
           "OUTPUT_SPEC = ASPECT_RATIOS[ASPECT_RATIO_DEFAULT][\"spec\"]",
           "OUTPUT_SPEC = ASPECT_RATIOS[\"9:16\"][\"spec\"]  # MUTANT", T_UI),
    Mutant("V4h 默认画幅被改成 9:16(存量内容重做一次就变样)", GEO_CONFIG,
           'ASPECT_RATIO_DEFAULT = "3:4"',
           'ASPECT_RATIO_DEFAULT = "9:16"  # MUTANT', T_V3),

    # ── V5 语料读取(真实语料实跑后发现的两个缺陷)──
    Mutant("V5a 截断退回 SQL 侧(真实语料上服务端报 invalid byte sequence)", CORPUS_DB,
           "                   caption, digg_count",
           "                   left(caption, 320) AS caption, digg_count  # MUTANT",
           T_V3),
    # 🔴 第一版这条变异写的是"在第二条查询前插一句 raise" —— 那**没有模型化这个缺陷**:
    #    三条查询仍然都在 try 里,结构判据看不出任何变化,于是变异存活。
    #    真正的缺陷形态是"兜底接得不够宽"(或只兜第一条),所以要改 except 的类型。
    Mutant("V5b 兜底收窄(真实读失败穿出去 → 蒸馏端点 500)", CORPUS_DB,
           "    except Exception as e:  # noqa: BLE001 - 表没建/读失败都不该让蒸馏整个挂掉",
           "    except ZeroDivisionError as e:  # MUTANT", T_V3),

    # ── V6 蒸馏 LLM 预算 + 前端错误人话(Owner 实测撞到)──
    # 🔴 2026-08-05 改异步后 V6a **换了方向**。原来变异的是"调大"(同步时代
    #    调大 = 撞 nginx 60s = HTML 504 + 扣费)。异步之后墙没了,调大不再是那个缺陷;
    #    **真正的缺陷方向变成了"调小"** —— 预算 42 < 实测 58,后台任务每次被自己掐死,
    #    成功率仍 0/5(只是从"504+扣费"变成"礼貌失败+不扣费")。
    #    第一版交付就是把 42 原样留着,而当时的锁是 `0 < X <= 300`,42 照样过 →
    #    这条变异在 Review 全矩阵里**存活**,正是这么被抓出来的。
    Mutant("V6a 蒸馏预算调回同步时代的 42(< 实测 58s → 每次被自己掐死,仍 0/5)", DISTILL,
           "DISTILL_LLM_TIMEOUT_S = 120.0",
           "DISTILL_LLM_TIMEOUT_S = 42.0  # MUTANT", T_V3),
    # 成对的反向:上界也要有人守,否则 V6a 可以靠"设成 99999"假绿。
    Mutant("V6a2 蒸馏预算设成无限大(卡死的调用长时间锁住这个客户)", DISTILL,
           "DISTILL_LLM_TIMEOUT_S = 120.0",
           "DISTILL_LLM_TIMEOUT_S = 99999.0  # MUTANT", T_V3),
    # 跟随关系也要守:回收阈值写死成另一个数 → 改预算后正常任务跑着就被判死。
    Mutant("V6a3 超龄回收阈值写死(不再跟随预算 → 正常任务被误杀)", DISTILL_TASK,
           "    return float(DISTILL_LLM_TIMEOUT_S) + 60.0",
           "    return 102.0  # MUTANT", T_V3),
    Mutant("V6b 算了预算却不传(等于没修)", DISTILL,
           "    raw = await _call_llm(prompt, timeout_s=DISTILL_LLM_TIMEOUT_S, diag=diag)",
           "    raw = await _call_llm(prompt, diag=diag)  # MUTANT", T_V3),
    Mutant("V6c 顺手把异步生产链的超时也改小(整条链开始超时)", CONTENT_GEN,
           "_LLM_TIMEOUT_S = 180.0",
           "_LLM_TIMEOUT_S = 42.0  # MUTANT", T_V3),
    Mutant("V6d 前端退回直接展示 e.message(解析器报错给用户看)", ENTRY_TSX,
           "            setError(formatApiErrorForDisplay(e, '这次没蒸出选题，再试一次'));",
           "            setError(e instanceof Error ? e.message : '这次没蒸出选题，再试一次');  // MUTANT",
           T_V3),
    Mutant("V6e readApiJson 把 HTML 原文塞进 detail(换个地方继续给乱码)",
           ROOT / "frontend" / "src" / "lib" / "api.ts",
           "            data: isJson ? parsed : undefined,",
           "            data: isJson ? parsed : { detail: raw },  // MUTANT", T_V3),

    # ── V7 词来自客户买的那些(Owner 08-03)──
    Mutant("V7a 覆盖词漏进来(拿客户没买来产内容的词做图文)", DISTILL,
           "                      AND (ck.is_core IS NOT FALSE)",
           "                      -- MUTANT", T_V3),
    Mutant("V7b 报价状态闸丢掉(草稿报价的词也拿来做)", DISTILL,
           "                      AND q.status IN ('confirmed', 'paid')",
           "                      -- MUTANT", T_V3),
    Mutant("V7c 薄壳自己再写一条查询(两份实现必漂)", DISTILL,
           "    rows = await load_purchased_keywords(brand_id, limit=limit)",
           "    _sql = 'SELECT ck.keyword FROM confirmed_keywords ck'  # MUTANT\n"
           "    rows = await load_purchased_keywords(brand_id, limit=limit)", T_V3),
    Mutant("V7d 前端退回常驻空输入框(又让用户自己敲)", ENTRY_TSX,
           "                            {!kwLoading && (purchased.length === 0 || manualKw) && (",
           "                            {!kwLoading && true && (  // MUTANT", T_V3),
    Mutant("V7e 空态引导指向不存在的路由(死链)", ENTRY_TSX,
           '<a href="/pricing" className="ml-1 underline underline-offset-2">',
           '<a href="/quote-center" className="ml-1 underline underline-offset-2">  {/* MUTANT */}',
           T_V3),

    # ── V8 表达方式实证回写 + 规划层 ──
    Mutant("V8a 默认钩子退回疑问式(试点行业 -5.2pp)", CARD_TPL,
           'DEFAULT_HOOK = "warning"', 'DEFAULT_HOOK = "question"  # MUTANT', T_V3),
    Mutant("V8b 钩子不进 prompt(方法论又变回死常量)", CONTENT_GEN,
           "        hook_line=COVER_HOOKS[pick_hook(industry_key)],",
           "        hook_line='',  # MUTANT", T_V3),
    Mutant("V8c B端/C端 给同一段约束(实测结论没落地)", CARD_TPL,
           '    if is_b_side(industry_key):',
           '    if False:  # MUTANT', T_V3),
    Mutant("V8d 行业不传进生成侧(全站同一套话术)", PROD_TASK,
           "            industry_key=industry_key,", "", T_V3),
    Mutant("V8e 规划自己重算饱和曲线(两个口径打架)", CONTENT_PLAN,
           '        quota = max(1, int(row.get("required_articles") or 1))',
           "        import math  # MUTANT\n"
           "        quota = max(1, math.ceil(0.25 * 12 / 0.75))", T_V3),
    Mutant("V8f 做超了给负缺口(前端显示「还差 -2 条」)", CONTENT_PLAN,
           "        gap = max(0, quota - done)",
           "        gap = quota - done  # MUTANT", T_V3),

    # ── V9 城市来自客户资料,不给默认 ──
    Mutant("V9a 前端又写死一份默认城市", ENTRY_TSX,
           "    const [cityInput, setCityInput] = useState('');",
           "    const [cityInput, setCityInput] = useState('深圳、广州、杭州');  // MUTANT",
           T_V3),
    # 🔴 第一版打在循环开头那道 _NON_CITY 上 —— 那是 **no-op**:
    #    后面 `if t and t not in _NON_CITY` 会把它兜回来,所以变异存活。
    #    要打就打在**真正生效**的那道上。
    Mutant("V9b 把「全国」当成城市(会做出一组假城市内容)", DISTILL,
           "        if t and t not in _NON_CITY and t not in out and len(t) <= 8:",
           "        if t and t not in out and len(t) <= 8:  # MUTANT", T_V3),
    Mutant("V9c 不归到市一级(「曲靖市罗平县」整串当城市)", DISTILL,
           '            t = t.split("市", 1)[0]',
           '            pass  # MUTANT', T_V3),
    Mutant("V9d 不读客户档案只用报价单(违背「引用客户的资料」)", DISTILL,
           "    from_brand = parse_cities(raw)",
           "    from_brand = []  # MUTANT", T_V3),

    # ── V10 Owner 2026-08-04 看图提的四件事 ──
    Mutant("V10a 建帖端点收了 style_key 却不透传(收了不用)", DOUYIN_API,
           '        style_key=str(req.style_key or "").strip(),',
           "        # MUTANT: 收了不用", T_UI),
    Mutant("V10b 创作页选了风格但不发出去", ENTRY_TSX,
           "                    style_key: styleKey,",
           "                    // MUTANT: 不发", T_UI),
    Mutant("V10c produce 依赖退回只有 loadPosts(画幅/风格锁死首渲染旧值)",
           ENTRY_TSX,
           "    }, [loadPosts, brandId, aspectRatio, styleKey]);",
           "    }, [loadPosts]);  // MUTANT", T_UI),
    Mutant("V10d 手机机身不固定比例(高度又由图撑,回到 1:1.59)", DETAIL_TSX,
           '<div className="flex aspect-[9/19.5] flex-col overflow-hidden',
           '<div className="flex flex-col overflow-hidden', T_UI),
    Mutant("V10e 图不约束在屏幕区内(固定了 aspect 却让图撑破)", DETAIL_TSX,
           'className="block max-h-full max-w-full object-contain"',
           'className="block w-full object-contain"  // MUTANT', T_UI),
    Mutant("V10f 详情页又留一份本地样板图表(共用组件白抽)", DETAIL_TSX,
           "// 标题长度建议档",
           "const STYLE_SAMPLES: Record<string, string> = {};\n// 标题长度建议档",
           T_UI),
    Mutant("V10g 1 区复活成第 1 步(状态展示又顶着步骤号)", ENTRY_TSX,
           '<h3 className="text-sm font-semibold">想让客户搜什么</h3>',
           '<h3 className="text-sm font-semibold">给哪个客户做</h3>', T_UI),
    Mutant("V10h 风格选择器不渲染样板图(退回四个纯文字按钮)", STYLE_PICKER,
           "{STYLE_SAMPLES[s.key] && (",
           "{false && (  // MUTANT", T_UI),
    Mutant("V10i 闸提示丢掉 gateReason 判定(渲染只有感叹号的空黄框)", DETAIL_TSX,
           "{!canPublish && gateReason && (",
           "{!canPublish && (  // MUTANT", T_UI),
    Mutant("V10j 缺口块没做够/做够都显示(出现「还差 0 条」)", ENTRY_TSX,
           "{plan.length > 0 && plan.some(p => p.gap > 0) && (",
           "{plan.length > 0 && (  // MUTANT", T_UI),

    # ── V11 2026-08-04 返工(Review 攻破 §3.1 + 验收实测到的重复扣费)──
    Mutant("V11a 实拍资格退回旧粒度(logo-only 被当成有实拍图→模型编假实景)",
           KB_CTX,
           "        return bool(self.image_hints)",
           "        return bool(self.logo_hints or self.image_hints)  # MUTANT", T_V3),
    Mutant("V11b 生产侧又回去读展示用的 kb_sources", PROD_TASK,
           "            has_authorized_photo=bool(getattr(content, \"has_real_photo\", False)),",
           "            has_authorized_photo=\"brand_image_assets\" in (content.kb_sources or []),",
           T_V3),
    Mutant("V11c 专用信号不从上下文带出来(默认 False→有实拍图也被降级)",
           CONTENT_GEN,
           "        has_real_photo=ctx.has_real_photo,",
           "        # MUTANT: 不带", T_V3),
    # 🔴 2026-08-05 改异步后这条更要命:请求 2 秒就返回,占位若不真占上,连点两下=两次 130。
    Mutant("V11d 蒸馏判定完不占位(连点两下 → 扣两次 130)", DISTILL_TASK,
           "    _INFLIGHT_BRANDS[int(brand_id)] = now\n    return True",
           "    pass  # MUTANT\n    return True", T_V3),
    Mutant("V11e in-flight 不自愈(进程被杀后这个客户永久锁死)", DISTILL_TASK,
           "        if now - started < stale_after_seconds():\n            return False",
           "        return False  # MUTANT", T_V3),
    Mutant("V11f 释放挪出 finally(异常/取消路径漏放→锁死客户)", DISTILL_TASK,
           "        _release_inflight(brand_id)",
           "        pass  # MUTANT", T_V3),
    # 🔴 2026-08-05 冷却窗概念随异步化取消(在飞期间直接 429,不再有"完成后再等 20s")。
    #    这一格换成守 DB 级兜底:进程内锁在 WORKERS>1 时失效,唯一索引是第二道防线。
    Mutant("V11g 在飞唯一索引不是 partial(客户只能蒸一次)", DISTILL_SQL,
           "    WHERE status IN ('pending', 'running');",
           "    ;  -- MUTANT", T_V3),

    # ── V12 2026-08-05 Owner P0:推荐客户 + 视觉身份按客户定制 ──
    Mutant("V12a 自荐段不传进 prompt(退回「品牌只是一行背景事实」)", CONTENT_GEN,
           "        promote_block=promote_block(brand_name or ctx.brand_name,",
           "        promote_block=\"\",  # MUTANT\n        _unused=(lambda *a, **k: '')(", T_V3),
    Mutant("V12b 不点名客户也放行(P0 事故复活)", CONTENT_GEN,
           "    if who and not out.promotes_brand:",
           "    if False:  # MUTANT", T_V3),
    Mutant("V12c 判点名时把 brand_line 也算进去以外还兜底塞名字(自己替客户编推荐语)",
           CONTENT_GEN,
           '        hay = f"{self.body}\\n{self.closing.get(\'brand_line\', \'\')}"',
           '        hay = f"{self.body}\\n{self.closing.get(\'brand_line\', \'\')}\\n{who}"  # MUTANT',
           T_V3),
    Mutant("V12d B端/C端 自荐措辞合并成一段(实证 7 倍差异没落地)", CARD_TPL,
           '    if is_b_side(industry_key):\n        stance = (f"以**亲自查过/对比过的人**的口吻写。"',
           '    if False:  # MUTANT\n        stance = (f"以**亲自查过/对比过的人**的口吻写。"',
           T_V3),
    Mutant("V12e 客户视觉素材又不进 prompt(视觉身份无从定制)", KB_CTX,
           '        if self.image_hints:\n            parts.append("【客户实拍图(可作为这一组的视觉场景依据)】"',
           '        if False:  # MUTANT\n            parts.append("【客户实拍图(可作为这一组的视觉场景依据)】"',
           T_V3),
    Mutant("V12f 场景不进共享段(七张各想一个场景,凑不成一套)", CARD_TPL,
           '    scene_line = (f"整组固定使用同一个真实场景：{style.scene}；',
           '    scene_line = (f"整组固定使用真实场景；', T_TPL),
    Mutant("V12g 强调色写死琥珀黄(所有客户长一个样)", CARD_TPL,
           "f\"小标签块用{style.accent_color}；",
           "f\"小标签块用琥珀黄；  # MUTANT", T_TPL),
    Mutant("V12h 视觉身份不从 LLM 取,只按行业(客户素材白读)", CARD_TPL,
           '    primary = str(v.get("primary_color") or "").strip()',
           '    primary = ""  # MUTANT', T_TPL),
    Mutant("V12i 行业兜底表退化成一个色(按行业定形同虚设)", CARD_TPL,
           "    return _INDUSTRY_PALETTE.get(str(industry_key or \"\").strip(), _PALETTE_FALLBACK)",
           "    return _PALETTE_FALLBACK  # MUTANT", T_TPL),
    Mutant("V12j 生产链回退到模板池造标题", PROD_TASK,
           "            title=content.title, body_text=content.body,\n"
           "            hashtags=content.hashtags, cards=content.cards,",
           "            title=__import__('services.geo_douyin.title_engine', fromlist=['x'])"
           ".build_title(keyword, city or None).title, body_text=content.body,\n"
           "            hashtags=content.hashtags, cards=content.cards,  # MUTANT", T_V3),
    Mutant("V12k AI 没给标题时静默继续(而不是明确失败)", PROD_TASK,
           '        if not content.title or not content.hashtags:\n'
           '            return await _fail("copy", "title_or_hashtags_missing")',
           "        pass  # MUTANT", T_V3),
    Mutant("V12m 顶栏又拼一次城市(印在图上的「深圳深圳…」)", CARD_TPL,
           '    head_kw = kw if (loc and kw.startswith(loc)) else f"{loc}{kw}"',
           '    head_kw = f"{loc}{kw}"  # MUTANT', T_TPL),
    Mutant("V12n 重抽不读快照视觉身份(重抽那张换配色)", REDRAW,
           "        visual=snapshot.get(\"visual\") if isinstance(snapshot.get(\"visual\"), dict) else None,",
           "        visual=None,  # MUTANT", T_V3),
    # ── W 系列 · 2026-08-06 生产堵塞排查包 ──
    # 每一条都对着一个**生产上真发生过**的失败,不是推演出来的。
    Mutant("W1a 客户名又去读那个不存在的列(brand_name)", GEO_API,
           '_BRAND_ROW_COLUMNS = ("name", "company_name")',
           "_BRAND_ROW_COLUMNS = ('brand_name',)  # MUTANT", T_V5),
    Mutant("W1b 取名字不走统一入口(四处各写各的又回来)", GEO_API,
           '    for key in _BRAND_ROW_COLUMNS + _BRAND_MAPPED_KEYS:',
           "    for key in ('brand_name',):  # MUTANT", T_V5),
    Mutant("W1c 没名字不当场拦(花完 390 才发现做不成)", GEO_API,
           '    if not brand_name:\n        raise HTTPException(status_code=400, detail={\n            "code": "BRAND_NAME_MISSING",',
           '    if False:  # MUTANT\n        raise HTTPException(status_code=400, detail={\n            "code": "BRAND_NAME_MISSING",', T_V5),
    Mutant("W2a 额度退回定值(撞顶就没有第二次机会)", CONTENT_GEN,
           'MAX_TOKENS_LADDER = (16000, 24000)',
           'MAX_TOKENS_LADDER = (16000,)  # MUTANT', T_V5),
    Mutant("W2b 撞顶仍记 success(观测面说成功、任务说失败)", CONTENT_GEN,
           '                    success=not truncated,',
           '                    success=True,  # MUTANT', T_V5),
    Mutant("W2c 撞顶不识别(退回统一报「服务暂时不可用」)", CONTENT_GEN,
           '                truncated = (not text) and finish == "length"',
           '                truncated = False  # MUTANT', T_V5),
    Mutant("W2d 文案链撞顶不单独报错码", CONTENT_GEN,
           '                error=("llm_truncated" if diag.get("reason") == "truncated"\n                       else "llm_unavailable"))',
           '                error="llm_unavailable")  # MUTANT', T_V5),
    Mutant("W2e 蒸馏链撞顶不单独报错码", DISTILL,
           '            error=("llm_truncated" if diag.get("reason") == "truncated"\n                   else "llm_unavailable"),',
           '            error="llm_unavailable",  # MUTANT', T_V5),
    Mutant("W2f 撞顶话术又落回「稍后重试」(无效动作)", GEO_API,
           '    ("llm_truncated", "文案写太长了，这次没收住；少做一两张、或把核心词写短一点再试"),',
           '    ("llm_truncated", "文案没写出来（写作服务暂时不可用），稍后重试"),  # MUTANT', T_V5),
    Mutant("W3a 灰按钮又不给理由(「点击了没用」复活)", ENTRY_TSX,
           'data-testid="distill-disabled-reason"',
           'data-testid="distill-disabled-reason-MUTANT"', T_V5),
    Mutant("W3b 进度又不显示(盯着不动的字等 100 秒)", ENTRY_TSX,
           'data-testid="distill-progress"',
           'data-testid="distill-progress-MUTANT"', T_V5),
    Mutant("W3c 不再取回上次蒸馏结果(切 tab 就丢,钱已扣)", ENTRY_TSX,
           '`/api/geo-douyin/clients/${brandId}/latest-topics`',
           '`/api/geo-douyin/clients/${brandId}/gone-MUTANT`', T_V5),
    Mutant("W3d 最近一次也回失败的那一行(半成品当成上次的选题)", GEO_DB,
           "                 WHERE brand_id = %s AND status = 'succeeded'",
           '                 WHERE brand_id = %s  -- MUTANT', T_V5),
    Mutant("W3e 只读端点不做归属校验(能读别人客户的选题)", GEO_API,
           '    _user(request)\n    require_brand_access(request, int(brand_id))\n\n    import asyncio\n\n    from db import geo_douyin_db as ddb\n\n    task = await asyncio.to_thread(ddb.get_latest_distill_task',
           '    _user(request)  # MUTANT\n\n    import asyncio\n\n    from db import geo_douyin_db as ddb\n\n    task = await asyncio.to_thread(ddb.get_latest_distill_task', T_V5),
    Mutant("W3f 批量下单失败了不停(继续发剩下的,重复骚扰)", ENTRY_TSX,
           '                    if (!ok) {',
           '                    if (false) {  // MUTANT', T_V5),
    Mutant("W3g 数量不收敛(NaN 条 / 一次几十条)", ENTRY_TSX,
           '    if (!Number.isFinite(n)) return 1;',
           '    // MUTANT', T_V5),
    Mutant("W3h 「先看看标题」端点复活", GEO_API,
           '@router.get("/clients/{brand_id}/latest-topics")',
           '@router.post("/title-variants")\nasync def api_title_variants(request: Request):  # MUTANT\n    return {"status": "success"}\n\n\n@router.get("/clients/{brand_id}/latest-topics")', T_V3),
    # ── W1 收敛(2026-08-06 返工)· hotfix 与 v5 各建过一个同用途单点函数 ──
    # 合并只保留一个,所以两侧的语义各要有一条变异盯着,少哪一半都得红。
    Mutant("W1d 合并时丢了 company_name 那一档(v5 的一半没了)", GEO_API,
           '_BRAND_ROW_COLUMNS = ("name", "company_name")',
           '_BRAND_ROW_COLUMNS = ("name",)  # MUTANT', T_V5),
    Mutant("W1e 合并时丢了 brand_name 兜底(hotfix 的一半没了)", GEO_API,
           '_BRAND_MAPPED_KEYS = ("brand_name",)',
           '_BRAND_MAPPED_KEYS = ()  # MUTANT', T_HOTFIX),
    Mutant("W1f 第二个同用途单点函数又回来了(其一必成死函数)", GEO_API,
           'async def fetch_brand_display_name(brand_id: Optional[int]) -> str:',
           'def brand_display_name(brand):  # MUTANT\n    return _brand_display_name(brand)\n\n\nasync def fetch_brand_display_name(brand_id: Optional[int]) -> str:', T_HOTFIX),
    # ── W4 · 拒绝自卖自夸(Owner 2026-08-06)──
    # 生产 post 18 写出了「我们全域上榜（深圳）科技有限公司…」
    # = 素人矩阵账号发自述广告,账号身份与口吻对不上。
    Mutant('W4a 自夸判定被摘掉(自述广告照样发出去)', CONTENT_GEN,
           '    hit = out.self_praise',
           "    hit = ''  # MUTANT", T_V5),
    Mutant('W4b 判废改成放行(自述内容当合格品交付)', CONTENT_GEN,
           '                                error="self_praise_voice",',
           '                                error="", ok=True,  # MUTANT', T_V5),
    Mutant('W4c 只扫正文不扫 brand_line(自夸挪到收尾句就绕过)', CONTENT_GEN,
           '        hay = f"{self.body}\\n{self.closing.get(\'brand_line\', \'\')}"\n        # ① 无条件禁',
           '        hay = self.body  # MUTANT\n        # ① 无条件禁', T_V5),
    Mutant('W4d 隔字类放宽到含「是」(误伤「我是XX的老客户」)', CONTENT_GEN,
           '                pat = re.compile(re.escape(pron) + r"[的，,、\\s]{0,2}"',
           '                pat = re.compile(re.escape(pron) + r"[的是这就，,、\\s]{0,3}"  # MUTANT', T_V5),
    Mutant('W4e prompt 退回「以服务方本人的口吻」(事前不拦,每单先烧 140 秒)', CARD_TPL,
           '        stance = (f"以**亲自查过/对比过的人**的口吻写。"',
           '        stance = (f"以**服务方本人**的口吻写(「我们」)。"  # MUTANT', T_V5),
    Mutant('W4f 点名要求被删(从打广告滑回不点名)', CARD_TPL,
           '        f"  1. **正文里必须出现「{who}」这个名字**,而且是**当成第三方来讲**;\\n"',
           '        f"  1. 可以提一下「{who}」。\\n"  # MUTANT', T_V5),
    # ── W5 · 截断按句边界(2026-08-06 · 生产 post 19 实证)──
    # 盲切把「…比如全域上榜（深圳）科技有限公司」砍成「比如全」印在图上;
    # 本机真跑 LLM 复测,第一条就切掉了「不承诺固定排名。」= 合规声明。
    Mutant('W5a 截断退回盲切(客户名被切成「比如全」)', CARD_TPL,
           '    for breaks in (_STRONG_BREAKS, _WEAK_BREAKS):',
           '    for breaks in ():  # MUTANT', T_V5),
    Mutant('W5b 名字被切掉仍交半句推荐语', CARD_TPL,
           '    if must and must in s and must not in out:\n        return ""',
           '    if False:  # MUTANT\n        return ""', T_V5),
    Mutant('W5c 收尾句截断不带 keep(名字随位置随机被切没)', CONTENT_GEN,
           '"brand_line": clip_text(closing_obj.get("brand_line"),\n                                         BRAND_LINE_MAX, keep=who)},',
           '"brand_line": clip_text(closing_obj.get("brand_line"), BRAND_LINE_MAX)},  # MUTANT', T_V5),
    Mutant('W5d 断点允许停在逗号(读着像没写完)', CARD_TPL,
           '            out = cut.rstrip(_WEAK_BREAKS) or cut',
           '            out = cut  # MUTANT', T_V5),
    Mutant('W5e 没有断点时不再硬切(整段超限放行)', CARD_TPL,
           '    if not out:\n        out = window.rstrip(_WEAK_BREAKS + _STRONG_BREAKS) or window',
           '    if not out:\n        out = s  # MUTANT', T_V5),
    Mutant('W5f 卡面要点退回盲切', CONTENT_GEN,
           '        points = [clip_text(x, 60) for x in pts if str(x).strip()][:4]',
           '        points = [str(x).strip()[:60] for x in pts if str(x).strip()][:4]  # MUTANT', T_V5),
    # ── W6 · 正文必须点名 + 空信息条不画(Owner 2026-08-06)──
    # post 19:正文通篇没客户名(名字只在收尾句和卡片上)却放行;
    #          第 4 张图底部一道空色块条 = footer 恒空还无条件画。
    Mutant('W6a 点名判据又算上收尾句(post 19 那个缝复活)', CONTENT_GEN,
           '        return who in str(self.body or "")',
           '        hay = f"{self.body}\\n{self.closing.get(\'brand_line\', \'\')}"\n        return who in hay  # MUTANT', T_V5),
    Mutant('W6b 收尾卡又画空信息条', CARD_TPL,
           '        + (f"底部一条{style.primary_color}色块条，写「{style.footer_bar}」。"\n           if style.footer_bar else "")',
           '        + f"底部一条{style.primary_color}色块条，写「{style.footer_bar}」。"  # MUTANT', T_V5),
    Mutant('W6c 内容卡又画空信息条', CARD_TPL,
           '        + (f"底部留一条窄信息条：「{style.footer_bar}」。"\n           if style.footer_bar else "")',
           '        + f"底部留一条窄信息条：「{style.footer_bar}」。"  # MUTANT', T_V5),
]


def _run(cmd: List[str]) -> int:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, cwd=str(ROOT)).returncode


def _verify(m: Mutant) -> int:
    if m.verify == "selftest":
        return _run([sys.executable, str(m.target), "--selftest"])
    return _run([sys.executable, "-m", "pytest", m.verify, "-q", "-x"])


def run() -> int:
    killed = 0
    total = 0
    problems: List[str] = []
    originals: Dict[Path, str] = {}

    # 基线必须全绿
    print("=== 基线 ===")
    for suite in {m.verify for m in MUTANTS}:
        code = (_run([sys.executable, str(ASR_PROBE), "--selftest"])
                if suite == "selftest"
                else _run([sys.executable, "-m", "pytest", suite, "-q"]))
        print(f"  {suite}: exit={code}")
        if code != 0:
            problems.append(f"基线不绿: {suite} —— 变异结果不可信")
    if problems:
        for p in problems:
            print("  🔴", p)
        return 1

    print("\n=== 变异 ===")
    for m in MUTANTS:
        total += 1
        if m.target not in originals:
            originals[m.target] = m.target.read_text(encoding="utf-8")
        orig = originals[m.target]
        mutated = orig.replace(m.old, m.new)
        if mutated == orig:
            problems.append(f"{m.name}: 🔴 锚点未命中源码(replace 无效)")
            print(f"  {m.name}: 🔴 锚点未命中")
            continue
        m.target.write_text(mutated, encoding="utf-8", newline="")
        try:
            code = _verify(m)
        finally:
            m.target.write_text(orig, encoding="utf-8", newline="")
        if code != 0:
            killed += 1
            print(f"  {m.name}: ✅ 被杀")
        else:
            problems.append(f"{m.name}: 🔴 存活 —— 该判据无判别力")
            print(f"  {m.name}: 🔴 存活")

    # 逐字节恢复断言
    for path, orig in originals.items():
        if path.read_text(encoding="utf-8") != orig:
            problems.append(f"{path.name}: 🔴 源码未恢复!")

    print(f"\n变异杀伤 {killed}/{total}")
    if problems:
        print("问题:")
        for p in problems:
            print("   -", p)
        return 1
    print("全部变异被杀 · 源码逐字节已恢复")
    return 0


def selftest() -> int:
    """核验 runner 自身:每个变异的锚点必须真命中源码。"""
    fails: List[str] = []
    seen: Dict[Path, str] = {}
    for m in MUTANTS:
        if not m.target.exists():
            fails.append(f"目标不存在: {m.target}")
            continue
        src = seen.setdefault(m.target, m.target.read_text(encoding="utf-8"))
        if m.old not in src:
            fails.append(f"锚点未命中源码: {m.name}")
        if m.verify != "selftest" and not (ROOT / m.verify).exists():
            fails.append(f"验证套件不存在: {m.verify}")
    if fails:
        print("[runner-selftest] 🔴 不通过:")
        for f in fails:
            print("   ", f)
        return 1
    print(f"[runner-selftest] ✅ {len(MUTANTS)} 个变异锚点全部命中源码,"
          f"验证套件齐全")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    raise SystemExit(selftest() if args.selftest else run())
