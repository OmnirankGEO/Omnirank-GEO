"""发布流向锁 · 详情页只到"去投放",发布全在发布中心

🔴 2026-08-03 Owner 裁定后重写。原来这里锁的是"详情页内嵌发布链"
   (选账号 → 频控 → 下单 → 绑回),而那条内嵌路径**已整体删除**。

   窄列表的根因(生产实测,写进 EXIT 防同型再犯):
     mhz_short_video 全表 20,061 → is_active 19,559(= Owner 说的 19,562 池)
     → platform='抖音' 5,852 → can_tuwen=1 **2,561**
   不是选错池,也不是漏筛选(那两刀是对的:池里 12 个平台、13,702 个号发不了图文),
   **真凶是漏分页** —— 合格账号 2,561 个,而端点 limit=50、前端拉一次就不再拉。
   等于在正确的计费链前面套了一个够不着完整池的窄窗口。

现在锁四件:
  1. 详情页**没有任何直接下单入口**(内嵌路径复活即红);
  2. 跳转必须带 post_id(不带 = 什么都没预填);
  3. 发布中心接收侧必须真预填(丢图即红);
  4. 闸关 / §15 补齐中 → 不放行去投放;用户可见文案不泄漏供应商名。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# 🔴 发布路径 2026-08-02 整页移进三栏详情页,旧的 DouyinPublishPanel.tsx 已删除。
#    锁必须跟着搬 —— 留在旧文件上就是"锁在死代码上":页面照常发布,锁照常绿。
PANEL = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"
# [WO_271 · 2026-09-23] 旧制作页 DouyinImagePost.tsx 已删(6b491ab23);现在挂详情页的是图文工作台路由
POST_PAGE = ROOT / "frontend" / "src" / "pages" / "Writing" / "ImageNoteDetailRoute.tsx"
API = ROOT / "api" / "geo_douyin_api.py"


def _panel() -> str:
    return PANEL.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# 1. 复用既有发布链,禁自建下单
# ─────────────────────────────────────────────────────────────
def test_detail_page_has_no_direct_ordering_path():
    """🔴 变异对①「内嵌下单路径复活 → 锁红」。

    详情页**绝不能**再直接打下单接口,也不能再自己攒 media_ids。
    发布中心才是唯一的下单入口(完整池 + 筛选 + 价格 + 频控 + 计费)。
    """
    src = _panel()
    for banned in ("/api/meijiehezi/short-video/publish",
                   "media_ids", "article_type: 3", "/api/geo-douyin/accounts",
                   "/api/geo-douyin/frequency-check"):
        assert banned not in src, f"详情页里内嵌下单路径复活了: {banned}"


def test_detail_page_jumps_with_post_id():
    """🔴 变异对②「跳转不带 post_id → 锁红」。

    不带 post_id,发布中心取不到图组与文案 —— 跳过去是一张空表单,
    用户还得自己重新上传 9 张图,等于这次改造白做。
    """
    src = _panel()
    assert "media_type=svideo" in src, "没跳到发布中心的短视频面板"
    assert "geo_post_id=${postId}" in src, "跳转没带 post_id"
    assert "douyin-goto-publish" in src, "没有「去发布投放」入口"


def test_publish_center_prefills_from_post():
    """🔴 变异对③「预填丢图 → 锁红」。

    接收侧必须真把图组/标题/正文喂进面板的表单 state。

    🔴 判据必须落在**那次调用**上,不能只断言 setter 名出现过 ——
       `setNoteImages` 本来就是 useState 的 setter 名,永远在文件里。
       变异实测:把预填那行删掉,只断言名字的锁照样绿(同一型我已踩五次以上)。
    """
    panel = (ROOT / "frontend" / "src" / "pages" / "Publishing"
             / "ShortVideoPanel.tsx").read_text(encoding="utf-8")
    assert "geoPostId" in panel, "面板没接收 post_id"
    assert "/api/geo-douyin/prepare-publish-media" in panel, "没去取图组"
    assert "setNoteImages(imgs" in panel, "预填没把图组写进表单(丢图)"
    assert "setTitle(String(d.title))" in panel, "预填没写标题"
    assert "setContent(String(d.content))" in panel, "预填没写正文"
    # 必须落到图文模式,否则 article_type 会是 1(发视频)
    assert "setPublishMethod('article')" in panel, "没切到图文笔记模式"


def test_publish_center_prefill_is_backward_compatible():
    """必须不命中面:不带 geo_post_id 时,面板行为与改造前一致。

    共享主人翁制:动别人的文件要保兼容 —— 预填整段挂在 `if (!geoPostId) return` 上。
    """
    panel = (ROOT / "frontend" / "src" / "pages" / "Publishing"
             / "ShortVideoPanel.tsx").read_text(encoding="utf-8")
    assert "if (!geoPostId || geoPrefilled.current) return;" in panel, \
        "预填没有做成「不带参数就整段不生效」"


def test_publish_result_binding_endpoint_still_exists():
    """归因锚仍在:发布中心下完单要能把结果绑回作品。

    🔴 绑回的**调用方**从详情页搬走了(详情页不再发布),但端点本身必须留着 ——
       删了它 = 验收 §7.2 永远查不到"这条内容发到哪去了"。
    """
    api = API.read_text(encoding="utf-8")
    assert "/publish-result" in api, "绑回端点被删了(归因锚断)"
    for field in ("published_url", "order_id", "item_ids"):
        assert field in api, f"绑回少了归因字段 {field}"


def test_panel_does_not_build_its_own_order_or_refund():
    """前端不得直接打下单/退款/扣费类端点(那是既有 lane 的事)。"""
    src = _panel()
    forbidden = ["/api/publish/orders", "refund", "deduct", "/api/geo-douyin/orders"]
    hits = [f for f in forbidden if f in src]
    assert not hits, f"前端自建了下单/退款调用: {hits}"


def test_panel_does_not_touch_legacy_upload_form():
    """旧短视频上传表单零改动 —— 本面板不得引用它的上传端点。"""
    src = _panel()
    for ep in ["/short-video/upload-policy", "/short-video/upload-video",
               "/short-video/upload-cover"]:
        assert ep not in src, f"碰了旧上传表单端点 {ep}"


def test_forbidden_detector_has_power():
    """必须不命中面:检测口径不能恒真。"""
    fake = "authFetch('/api/publish/orders', {})"
    assert any(f in fake for f in ["/api/publish/orders"])


# ─────────────────────────────────────────────────────────────
# 2. 🔴 总闸关 → 按钮不可用(含反向面)
# ─────────────────────────────────────────────────────────────
def _status_fn():
    """取 api_status 的源码文本。

    用 AST 定位而不是正则切 —— 正则切函数体既容易写错(我第一版就写出
    unbalanced parenthesis),也会被后续函数顺序变化搞乱。
    """
    import ast
    tree = ast.parse(API.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_status":
            return ast.unparse(node)
    raise AssertionError("找不到 api_status —— 锚点失效")


def test_status_requires_all_three_gates():
    """can_publish 必须是三闸【与】关系:制作闸 + 上传闸 + 发布闸。"""
    fn = _status_fn()
    assert "make_on and upload_on and publish_on" in fn, (
        "can_publish 不是三闸与关系 —— 任一闸关时仍会放行按钮")


def test_status_failclosed_when_gates_unreadable():
    """读不到发布侧闸门必须按【关】处理(fail-closed),不能默认放行。"""
    fn = _status_fn()
    assert "upload_on = publish_on = False" in fn, "读不到闸门时没有 fail-closed"


def test_jump_blocked_when_gate_closed_or_completing():
    """闸关 / 还没做好 / §15 补齐中 → 不放行去投放,且给出原因文案。"""
    src = _panel()
    assert "disabled={jumpBlocked}" in src, "按钮没绑 disabled"
    assert "gateReason" in src, "闸关时没有说明原因"
    m = re.search(r"const jumpBlocked = (.+?);", src, re.S)
    assert m, "找不到 jumpBlocked 定义"
    expr = m.group(1)
    for cond in ["!canPublish", "notReady", "completing"]:
        assert cond in expr, f"jumpBlocked 少了条件 {cond}"


def test_jump_enabled_path_exists():
    """反向面:闸开 + 内容 ready + 已补齐时必须【可用】。

    只断言"闸关时禁用"不够 —— 一个永远禁用的按钮也能过那条(恒真)。
    """
    src = _panel()
    m = re.search(r"const jumpBlocked = (.+?);", src, re.S)
    expr = m.group(1)
    assert "||" in expr, "jumpBlocked 不是条件组合,可能被写死"
    assert expr.strip() != "true", "按钮被写死禁用"


def test_completing_post_is_blocked_server_side_too():
    """🔴 §15:补齐中的组**后端也要拦**。

    前端 checklist 能被绕过(直接打接口),后端不能 ——
    缺图发出去是残组,而且那笔冻结还没 commit。

    🔴 判据打在**那个比较**上,不是"函数里出现过 STATUS_COMPLETING" ——
       它在函数内的 import 行里也出现,把 if 改成 `if False` 仍然命中(实测存活)。
    """
    import ast
    tree = ast.parse(API.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "api_prepare_publish_media")
    cmps = [c for c in ast.walk(fn) if isinstance(c, ast.Compare)]
    hit = [c for c in cmps
           if any(isinstance(op, ast.Eq) for op in c.ops)
           and "STATUS_COMPLETING" in ast.unparse(c)]
    assert hit, "备图端点没有「作品状态 == 补齐中」这个比较 → 拦截被架空"
    assert "POST_COMPLETING" in ast.unparse(fn), "没给出可识别的错误码"


def test_gate_reason_is_vendor_neutral():
    """闸关文案不得出现供应商名(供应商零暴露)。"""
    fn = _status_fn()
    for vendor in ["媒介盒子", "快易播", "kuaiyibo"]:
        assert vendor not in fn, f"闸关文案泄漏供应商名: {vendor}"
    assert "外部发布通道" in fn, "应使用中性说法「外部发布通道」"


# ─────────────────────────────────────────────────────────────
# 3. 归因锚
# ─────────────────────────────────────────────────────────────
# 🔴 原来这里断言"详情页里有 published_url"。发布搬走之后详情页**不该**再有它
#    —— 那条锁如果留着,等于逼着详情页把发布代码留下来。
#    归因锚的锁已改到端点侧:test_publish_result_binding_endpoint_still_exists。


def test_post_page_mounts_detail_page():
    """制作页必须挂详情页 —— 发布入口现在在它里面。"""
    src = POST_PAGE.read_text(encoding="utf-8")
    # [WO_271] 判据落在**渲染点**(带尖括号),只留 import 不算挂上
    assert "<DouyinPostDetail " in src, "制作页没挂详情页"


def test_old_publish_panel_is_gone():
    """反向面:旧发布面板必须真的不在了。

    🔴 它零引用地留在仓库里最危险 —— G3/G4/G5 三条变异锁原本打在它身上,
       文件还在锁就照常绿,而真正跑的那条发布路径一条锁都没有。
    """
    old = ROOT / "frontend" / "src" / "pages" / "Writing" / "DouyinPublishPanel.tsx"
    assert not old.exists(), "旧发布面板还在 —— 锁会打在死代码上"


# ─────────────────────────────────────────────────────────────
# 4. 用户可见文案不泄漏供应商
# ─────────────────────────────────────────────────────────────
def test_panel_has_no_vendor_names():
    src = _panel()
    for vendor in ["媒介盒子", "快易播", "kuaiyibo.cn"]:
        assert vendor not in src, f"前端文案泄漏供应商名: {vendor}"


def test_panel_reads_backend_scrubbed_detail_only():
    """错误文案只取后端已脱敏的 detail,不把原始 body/异常抛给用户。"""
    src = _panel()
    assert "readError" in src, "没有统一的错误读取收口"
    # 不得直接把 response.text() 当错误展示
    assert "res.text()" not in src, "直接展示原始响应体会泄漏上游信息"


@pytest.mark.parametrize("endpoint", [
    "/api/geo-douyin/accounts",
    "/api/geo-douyin/frequency-check",
    "/api/geo-douyin/prepare-publish-media",
    "/api/geo-douyin/publish-result",
    "/api/geo-douyin/status",
])
def test_all_used_endpoints_exist_in_router(endpoint):
    """前端打的每个本包端点都必须真存在(防打到 404)。"""
    api_src = API.read_text(encoding="utf-8")
    path = endpoint.replace("/api/geo-douyin", "")
    assert f'"{path}"' in api_src, f"路由 {endpoint} 不存在"
