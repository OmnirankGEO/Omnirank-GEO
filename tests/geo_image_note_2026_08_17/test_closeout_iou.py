"""收尾轮 · Review 六条欠条的判据(2026-08-17)。

① retract 同步下架 · ② production draft 接线 · ③ 第二页卡级动作真调用
④ axe 基线 · ⑤ D10 门户冻结 fallback 5xx 注入 · ⑥ raw-attempt 标签

每条都配反向对照:只写正向的判据分不出"逻辑对"与"恒真"。
"""
from __future__ import annotations

import ast
import io
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return io.open(REPO / rel, encoding="utf-8", errors="ignore").read()


def _strip_ts_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


# ---------------------------------------------------------------------------
# ① retract 同步下架
# ---------------------------------------------------------------------------

def test_customer_delivery_plan_carries_canonical_stages():
    """selection token / 小榜 / 交付计划读的必须是**会随撤稿回落**的那个数。

    gap 侧 `evidence_published` 是只增不减的布尔(schema 有单调 CHECK),
    撤稿在它上面永远不生效 —— 客户会一直看到"已发布"。
    """
    tree = ast.parse(_src("api/selection_api.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_build_customer_delivery_plan")
    body = ast.unparse(fn)
    assert "quote_stage_tuple" in body, "客户交付计划没挂 canonical 投影"
    assert "canonical_stages" in body
    assert "retraction_notice" in body, "撤稿后没有任何对客户可见的说明"


def test_retraction_notice_does_not_hide_history_or_claim_refund():
    """01 §5.2 末:下架要显示「曾发布 · 现已下架」,不退成"未发布"、不自动声称退款。"""
    body = ast.unparse(next(
        n for n in ast.walk(ast.parse(_src("api/selection_api.py")))
        if isinstance(n, ast.FunctionDef) and n.name == "_build_customer_delivery_plan"))
    assert "曾发布" in body and "现已下架" in body
    for banned in ("已退款", "自动退款", "未发布"):
        assert banned not in body, f"下架文案出现了不该有的说法:{banned}"


def test_gap_evidence_is_not_the_published_truth_anymore():
    """反向对照:demote 标记还在,且 canonical 入口仍然存在。"""
    src = _src("db/gap_plan_db.py")
    assert "is_canonical_source" in src and "canonical_stage_tuple" in src


# ---------------------------------------------------------------------------
# ② production draft 接线
# ---------------------------------------------------------------------------

# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_batch_page_saves_draft_with_etag_and_shows_conflict：ImageNoteBatch 草稿/ETag 面随 #150 §4 整体撤销（新页 draft/etag 均 0 命中）



# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_draft_conflict_does_not_silently_overwrite：同上：草稿冲突面不存在



# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_draft_save_is_debounced：同上：草稿保存面不存在



# ---------------------------------------------------------------------------
# ③ 第二页卡级动作真调用
# ---------------------------------------------------------------------------

def test_card_actions_hit_three_distinct_endpoints():
    """01 §4.3:三个动作影响范围不同,所以是三个端点,不是一个接口加参数。"""
    # [WO_271] 校样页 ImageNoteProofRoute 随 #203 删除(cd4aa3bf1),「点进去那一屏」接回三栏详情页。
    #   那里「换风格」并进了再次创作的弹窗(同一个整条重做端点,带 style_key),卡级重抽仍是单独端点。
    code = _strip_ts_comments(_src("frontend/src/pages/Writing/DouyinPostDetail.tsx"))
    assert "/cards/${activeIdx}/redraw" in code, "重做一张没接线"
    assert "/regenerate" in code and "style_key: regenStyle" in code, "再次创作 / 换风格没接线"
    # 反向对照:回调不许再是空实现
    assert "/* 卡级重做由 WP3 的生成链承接 */" not in code


def test_card_actions_reload_from_server_not_local_guess():
    """成功后必须重新 load:前端拼一个"看起来像新版本"的对象,
    会与下一次保存的 If-Match 打架。"""
    # [WO_271] 改指三栏详情页:重抽 / 再次创作是异步的,后端说到终态就从服务端重读详情;
    #   开关类同步动作成功后同样重读 —— 都不在前端拼一个「看起来像新版本」的对象
    code = _strip_ts_comments(_src("frontend/src/pages/Writing/DouyinPostDetail.tsx"))
    assert "void loadDetail(busyKind === 'redraw');" in code, "重抽 / 再次创作到终态后没从服务端重读"
    assert "await loadDetail(true);" in code, "同步动作成功后没从服务端重读"


def _failure_not_announced(code: str) -> bool:
    """出错块没有播报给读屏(role=alert / aria-live 都没有)⇒ True。"""
    m = re.search(r'<div role="alert" data-testid="detail-action-error"', code)
    return not (m or 'aria-live="polite"' in code)


def test_action_failure_is_announced_not_swallowed():
    """[WO_271] 改指三栏详情页:校样页的动作状态条带 `aria-live="polite"`;并回详情页之后,
    动作失败只是一段普通文字(`{error}`),没有 aria-live / role="alert" —— 读屏用户听不到失败。
    [WO_283-F8 已修] 详情页的动作失败块 `role="alert"`(data-testid="detail-action-error")。
    """
    code = _strip_ts_comments(_src("frontend/src/pages/Writing/DouyinPostDetail.tsx"))
    assert not _failure_not_announced(code), "动作失败没有播报给读屏"


def test_action_failure_lock_has_power():
    """反臂(WO_283):把 role="alert" 拿掉 ⇒ 上一格必须报;现役源码不报(对照)。"""
    code = _strip_ts_comments(_src("frontend/src/pages/Writing/DouyinPostDetail.tsx"))
    assert not _failure_not_announced(code)
    assert _failure_not_announced(code.replace('<div role="alert" data-testid="detail-action-error"',
                                               '<div data-testid="detail-action-error"'))


# ---------------------------------------------------------------------------
# ④ axe 基线
# ---------------------------------------------------------------------------

def test_axe_dependency_is_declared_not_only_installed():
    """🔴 `npm install --no-save` 装上的东西在别的机器上不存在。
    判据必须靠**声明**成立,不能靠"我这台机器刚好装了"。"""
    import json

    pkg = json.loads(_src("frontend/package.json"))
    dev = pkg.get("devDependencies") or {}
    assert "@axe-core/playwright" in dev, "axe 只在本机装了,没进 package.json"
    lock = _src("frontend/package-lock.json")
    assert "@axe-core/playwright" in lock, "lock 没更新 —— npm ci 装不到"


def test_axe_spec_exists_and_asserts_zero_critical():
    spec = _src("frontend/tests/image-note/image-note-a11y.spec.ts")
    assert "AxeBuilder" in spec
    assert "critical" in spec and "serious" in spec
    # 反向对照:不许把断言写成"总有 0 条违规"那种恒真形态
    assert "toEqual([])" in spec or "toHaveLength(0)" in spec


def test_axe_baseline_is_scoped_to_this_package_and_declares_legacy():
    """🔴 扫全 document 会把**别人的存量红**算进本包基线 ——
    那样基线永远不可能绿,而长期红的判据等于没有判据。

    但存量红也不许静默过滤:静默过滤与从来没测过在结果上一模一样。
    形态要求:①扫描 `.include(<本包容器>)`;②另有一条把全页违规**打印出来**。
    """
    spec = _src("frontend/tests/image-note/image-note-a11y.spec.ts")
    assert ".include(selector)" in spec, "axe 没有限定作用域"
    for testid in ("image-note-batch", "image-note-proof", "image-note-panel"):
        assert f'[data-testid="{testid}"]' in spec, f"{testid} 没被单独扫"
    assert "console.log('[a11y]" in spec, "存量违规被静默吞掉了"


def test_axe_probe_has_discriminating_power():
    """三条全绿也可能是 axe 根本没跑起来 —— 必须有注入已知违规的自证。"""
    spec = _src("frontend/tests/image-note/image-note-a11y.spec.ts")
    assert "__axe_probe__" in spec
    assert "toBeGreaterThan(0)" in spec


# ---------------------------------------------------------------------------
# ⑤ D10 门户冻结 fallback
# ---------------------------------------------------------------------------

def test_frozen_report_body_exists_and_carries_everything():
    from services.publication_stage_adapters import frozen_report_body
    from services.publication_stage_projection import freeze_projection, source_versions

    frozen = freeze_projection(
        {"quote_id": 5, "cutoff": "2026-08-17T00:00:00+00:00",
         "stages": {"published_active": {"count": 2, "available": True, "reason": None}},
         "source_versions": source_versions()},
        watermark={"attempt_rows": 3})
    for key in ("quote_id", "cutoff", "stages", "source_versions",
                "watermark", "reproducibility_hash"):
        assert key in frozen, f"冻结体缺 {key} —— 事后无法判断两份报告能不能比"
    assert callable(frozen_report_body)


def test_portal_live_handler_failure_falls_back_to_projection_not_to_zero():
    """D10:live handler 5xx 时走冻结 fallback。

    🔴 判据打的是**形态**:门户端点里投影失败只降级 `contract_stages=None`,
       **绝不**把它糊成 0 —— 0 与"不知道"在界面上长得一样但含义相反。
    """
    tree = ast.parse(_src("server.py"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "get_publications_list")
    body = ast.unparse(fn)
    assert "stages_payload = None" in body
    assert "contract_stages" in body
    # 反向:不许出现把阶段糊成 0 的兜底
    assert "stages_payload = 0" not in body
    assert "'count': 0" not in body


def test_projection_unavailable_is_loud_not_a_zero():
    from services.publication_stage_projection import ProjectionUnavailable
    from services.publication_stage_sources import load_quote_projection

    with pytest.raises(ProjectionUnavailable):
        load_quote_projection(object(), quote_id=0)


def test_stage_value_unavailable_never_reports_a_count():
    from services.publication_stage_projection import StageValue

    v = StageValue.unavailable("capacity_not_issued")
    assert v.count is None and v.available is False
    # 反向对照:可用时确实给得出数
    assert StageValue(count=0).as_dict() == {"count": 0, "available": True, "reason": None}


# ---------------------------------------------------------------------------
# ⑥ raw-attempt 标签
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel,testid", [
    ("frontend/src/pages/Publishing/PublishHistory.tsx", "history-basis"),
    ("frontend/src/pages/Publishing/PublishCenter.tsx", "publish-center-basis"),
])
def test_raw_attempt_pages_say_so(rel, testid):
    code = _strip_ts_comments(_src(rel))
    assert testid in code, f"{rel} 没有标注计数口径"
    assert "未按合同去重" in code


def test_backend_already_ships_the_basis_field():
    """前端标签不是自说自话:后端同一个端点已经给了 `records_basis`。"""
    body = ast.unparse(next(
        n for n in ast.walk(ast.parse(_src("server.py")))
        if isinstance(n, ast.FunctionDef) and n.name == "get_publications_list"))
    assert "'records_basis': 'raw_attempts'" in body or '"records_basis": "raw_attempts"' in body


# ---------------------------------------------------------------------------
# 账号数据源(Review 另行交待的一项)
# ---------------------------------------------------------------------------

# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_account_catalog_reuses_existing_endpoint_no_route_growth：ImageNotePanel 发布面已撤



#: 本 lane 的**完整**路由面。集合式而不是计数式 ——
#:
#: 🔴 [返工 2026-08-18] 原来这条锁写的是 `len(routes) == 7`。
#:    本轮按规格 §7.5 / P0-04 补了 command GET 与 retry 两个端点，它当场红。
#:    「计数式判据会被正常业务写过期，集合式不会」：数字变成 9 之后，
#:    下一个人只会把 7 改成 9，而**没有任何一步在问"多出来的是哪两条"**。
#:    换成集合之后，加一条没登记的路由仍然红，而登记这个动作本身
#:    就强制了一次"这条路由该不该存在"的显式复核。
IMAGE_NOTE_ROUTE_SURFACE = {
    "/api/geo-douyin/production-preview",
    "/api/geo-douyin/production-drafts/{draft_id}",
    "/api/geo-douyin/batches",
    "/api/geo-douyin/batches/{batch_id}",
    "/api/meijiehezi/image-notes/publish-preview",
    "/api/meijiehezi/image-notes/publish-batch",
    # 返工 2026-08-18 · P0-04 后半：command 动态状态与失败项重投
    "/api/meijiehezi/image-notes/commands/{command_id}",
    "/api/meijiehezi/image-notes/commands/{command_id}/retry",
    "/api/geo-douyin/posts/{post_id}/prepare-publish-media-v2",
}


def test_account_route_surface_is_exactly_the_registered_set():
    """本包的路由面必须**逐条**等于登记集合（不因账号目录等原因悄悄变大）。"""
    src = _src("api/geo_image_note_api.py")
    routes = re.findall(r'@router\.(?:get|post|put|patch)\("([^"]+)"', src)
    assert routes, "结构锚零命中 —— 判据没有分母"
    extra = sorted(set(routes) - IMAGE_NOTE_ROUTE_SURFACE)
    missing = sorted(IMAGE_NOTE_ROUTE_SURFACE - set(routes))
    assert not extra, f"新增了未登记的路由: {extra}"
    assert not missing, f"登记过的路由不见了: {missing}"
    # 🔴 同一路径两个方法算两条；集合会把它们并成一条，所以再比一次条数，
    #    防"把一条 POST 悄悄改成同路径的 GET"这种集合看不见的变化。
    assert len(routes) == len(set(routes)), f"同一路径注册了多次: {routes}"


# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_eligibility_comes_from_the_server_not_the_frontend：publish-preview 资格面已撤（0 命中）
