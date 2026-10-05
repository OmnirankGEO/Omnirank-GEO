"""判别测试 · 移动端支付「点了没反应」(WO_MOBILE_PAY_JUMP_2026-08-07)。

工单坐实的两个案例(2026-08-07 上午生产只读实测,不是推断):

  案例 A · 用户 154 · **Mac 桌面微信**(UA 含 ``MacWechat/UnifiedPCMacWechat``)
    /customer/recharge → 路由 **wechat_jsapi** → jsapi/create 返 200 → ¥138 一直 pending;
    3 分钟后换**安卓微信**,¥100 走同一条 jsapi 路 **11 秒付成**。
    → 判据不是"能不能下单"(下单一直是 200),是**付款层弹不弹得出来**。
      桌面微信的 WeixinJSBridge 拉起不可靠,表现正是"点了没反应"。

  案例 B · 用户 125 · **iPhone iOS18.7 Safari(非微信)**
    /agent/inventory 进货 → 虎皮椒下单成功(``h5=True``,payment_url_mobile 已返回)
    → AIP70C 仍 pending,只见 order-status 轮询、无跳转迹象;重试才付成。
    → 服务端证据链**到"H5 链接已交到前端"为止全绿**;跳去 xunhupay 外域不经过我们的日志,
      服务端对"用户跳没跳出去"是**零可见度**的 —— 所以 §2.1 的三个埋点是其余修理的前提。

两个通道当天**都有成功支付**,资金零损失。这是转化杀手(首试败 + 重试成),不是通道宕机。

🔴 本文件每一条"必须命中"都配了成对的"必须不命中" —— 判据退化成恒真比漏检更坏
   (2026-08-02 死函数判据恒绿 / 2026-08-06 一天坏十几条判据,病根都是这个)。

跑法:
    PYTHONUTF8=1 python -m pytest -q tests/test_mobile_pay_jump_2026_08_07.py
变异自检(证明这些断言有判别力):
    PYTHONUTF8=1 python tests/mutation_runner_mobile_pay_jump_2026_08_07.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.payment_routing import (  # noqa: E402
    DESKTOP_WECHAT_UA_TOKENS,
    MOBILE_UA_TOKENS,
    WECHAT_JSAPI,
    WECHAT_NATIVE,
    XUNHUPAY,
    detect_payment_channel,
    is_desktop_wechat_browser,
    is_mobile_wechat_browser,
    is_wechat_browser,
    resolve_payment_channel,
)

# ── 生产实测 UA(工单 §1 两个案例的真实形态,勿改)──────────────────────────
UA_MAC_WECHAT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.3 Safari/605.1.15 "
    "MicroMessenger/6.8.0(0x16080000) MacWechat/3.8.6(0x13080610) "
    "UnifiedPCMacWechat(0xf2640611) Concurrent"
)
UA_WINDOWS_WECHAT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/107.0.0.0 Safari/537.36 NetType/WIFI "
    "MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090c11) XWEB/8391"
)
UA_ANDROID_WECHAT = (
    "Mozilla/5.0 (Linux; Android 13; PGT-AN10 Build/HUAWEIPGT-AN10; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/107.0.5304.141 "
    "Mobile Safari/537.36 MMWEBID/1234 MicroMessenger/8.0.42.2460(0x28002A35) WeChat/arm64"
)
UA_IOS_WECHAT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.42(0x18002a2f) NetType/WIFI Language/zh_CN"
)
UA_IOS_SAFARI = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)
UA_MAC_SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
UA_WINDOWS_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

FRONTEND = REPO / "frontend" / "src"
PAY_ENV_TS = FRONTEND / "lib" / "paymentEnv.ts"
PAY_TELEMETRY_TS = FRONTEND / "lib" / "paymentTelemetry.ts"
ANALYTICS_TS = FRONTEND / "lib" / "analytics.ts"
INVENTORY_TSX = FRONTEND / "pages" / "Agent" / "InventoryCenter.tsx"
BUYCREDIT_TSX = FRONTEND / "pages" / "Customer" / "BuyCredit.tsx"

# 🔴 [#169 换锚 · 2026-09-10] 三个支付面的「拿到 payment_url_mobile 之后怎么出去」
#    已收进 PayExit.tsx(#169 §2.1.1 一套实现)。支付 anchor 现在只在那一个文件里。
#    下面两条锁**语义一字未改**,只是分母跟着代码走:
#      · 「移动端不许无条件 _blank」问的是**所有**支付 anchor,不是「某个文件里的」;
#      · 「三事件都接线」拆成两半 —— 页面级(shown / 装监听)与出口级(clicked / arm),
#        并补上原来没有的那条边:页面必须把 armJumpTracking **传进** PayExit。
#    🔴 不删用例、不放宽:重锚后比原来更严(多钉了一条边)。
#    🔴 顺带说清:#169 搬代码时**先红的正是这条锁自己的反向对照**
#       (「找不到支付 anchor 时循环会空转即通过」)。它把「锚失效」与「没有缺陷」
#       分开了 —— 没有那句,这次搬家会让这两条锁**静静变绿**。
PAYEXIT_TSX = FRONTEND / "components" / "payment" / "PayExit.tsx"
PAY_SURFACES = [INVENTORY_TSX, BUYCREDIT_TSX, PAYEXIT_TSX]


def _strip_comments(src: str) -> str:
    """🔴 去注释后再做文本判定。

    2026-08-05/06 两次实测:判据抓到的是**我自己写的注释**,把代码删掉照样绿。
    凡"某处必须有这行代码"的判据,一律打在去注释后的文本上。
    """
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"(^|[^:])//[^\n]*", r"\1", src)
    return src


def _code(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _py_string_literals(path: Path) -> list[str]:
    """Python 源码里**真正被当值用**的字符串常量(剔掉 module/class/def 的 docstring)。

    按整份文本 grep 会被文档本身骗到 —— 三个下单入口的 docstring 里都写着
    "UA 含 MicroMessenger"。判据要打在代码上,不是打在对代码的描述上。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


# ════════════════════════════════════════════════════════════════════════════
# §2.2 桌面微信不再路由 JSAPI(案例 A)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("ua", [UA_MAC_WECHAT, UA_WINDOWS_WECHAT])
def test_desktop_wechat_routes_to_native_not_jsapi(ua: str) -> None:
    """必须命中:桌面微信 → native(扫码),不再进 jsapi。"""
    assert detect_payment_channel(ua) == WECHAT_NATIVE
    assert is_desktop_wechat_browser(ua) is True
    assert is_mobile_wechat_browser(ua) is False


@pytest.mark.parametrize("ua", [UA_ANDROID_WECHAT, UA_IOS_WECHAT])
def test_mobile_wechat_jsapi_is_byte_for_byte_unchanged(ua: str) -> None:
    """🔴 必须不命中:工单 §3 明令「不许把移动微信一起改掉」——

    案例 A 里安卓微信 11 秒付成,那条路一个字都不能动。
    """
    assert detect_payment_channel(ua) == WECHAT_JSAPI
    assert is_desktop_wechat_browser(ua) is False
    assert is_mobile_wechat_browser(ua) is True


def test_desktop_wechat_check_is_not_a_blanket_wechat_check() -> None:
    """反向对照:桌面判据不能退化成"只要是微信就算桌面"(那样移动微信会被误伤)。"""
    assert is_wechat_browser(UA_ANDROID_WECHAT) is True     # 仍是微信
    assert is_desktop_wechat_browser(UA_ANDROID_WECHAT) is False   # 但不是桌面微信
    # 也不能退化成"只要是桌面就算桌面微信"
    assert is_desktop_wechat_browser(UA_MAC_SAFARI) is False
    assert is_desktop_wechat_browser(UA_WINDOWS_CHROME) is False


def test_unified_pc_variants_are_covered_by_the_two_tokens() -> None:
    """UnifiedPCMacWechat / UnifiedPCWindowsWechat 是两个 token 的超串 —— 不需要单列。

    这条锁的作用是:哪天有人"精简"掉 macwechat / windowswechat,超串覆盖就断了。
    """
    assert set(DESKTOP_WECHAT_UA_TOKENS) == {"windowswechat", "macwechat"}
    assert "macwechat" in "unifiedpcmacwechat"
    assert "windowswechat" in "unifiedpcwindowswechat"
    for token in DESKTOP_WECHAT_UA_TOKENS:
        assert token == token.lower(), "token 必须是小写 · 判据用的是 ua.lower()"


def test_desktop_wechat_rule_wins_over_wechat_rule_by_order() -> None:
    """桌面微信同时满足 is_wechat_browser —— 顺序错了这条修复就等于没做。"""
    assert is_wechat_browser(UA_MAC_WECHAT) is True   # 它确实是微信
    assert detect_payment_channel(UA_MAC_WECHAT) == WECHAT_NATIVE  # 但先被摘出去了


def test_other_channels_unchanged() -> None:
    """必须不命中:非桌面微信那三条路由**逐字不变**。"""
    assert detect_payment_channel(UA_IOS_SAFARI) == XUNHUPAY        # 手机非微信 → 虎皮椒
    assert detect_payment_channel(UA_MAC_SAFARI) == WECHAT_NATIVE   # PC → 扫码
    assert detect_payment_channel(UA_WINDOWS_CHROME) == WECHAT_NATIVE
    assert detect_payment_channel("") == WECHAT_NATIVE
    assert detect_payment_channel(None) == WECHAT_NATIVE


def test_desktop_wechat_is_not_counted_as_mobile() -> None:
    """桌面微信不许被 MOBILE_UA_TOKENS 顺手捞成手机(否则它会掉进 xunhupay)。"""
    assert detect_payment_channel(UA_MAC_WECHAT) != XUNHUPAY
    assert detect_payment_channel(UA_WINDOWS_WECHAT) != XUNHUPAY


def test_explicit_xunhupay_still_blocked_inside_any_wechat() -> None:
    """🔴 故意不跟着改:这条闸挡的是"微信生态内不许显式点第三方支付链接",

    桌面微信同样在微信生态内。detect 把桌面微信当 PC 的理由是 JSAPI 拉不起来,
    两件事理由不同,不要顺手对齐(对齐了会把私账 H5 链接送进微信内)。
    """
    for ua in (UA_MAC_WECHAT, UA_WINDOWS_WECHAT, UA_ANDROID_WECHAT):
        with pytest.raises(ValueError):
            resolve_payment_channel("xunhupay", ua)
    # 反向对照:非微信 PC / 手机显式点虎皮椒仍然放行(闸不是恒抛)
    assert resolve_payment_channel("xunhupay", UA_IOS_SAFARI) == XUNHUPAY
    assert resolve_payment_channel("xunhupay", UA_WINDOWS_CHROME) == XUNHUPAY


def test_force_xunhupay_switch_still_wins() -> None:
    """紧急回滚开关不受本次改动影响。"""
    assert resolve_payment_channel("auto", UA_MAC_WECHAT, force_xunhupay=True) == XUNHUPAY


def test_auto_resolution_goes_through_the_same_detector() -> None:
    """resolve('auto') 必须走 detect —— 否则两个入口会各修各的。"""
    for ua in (UA_MAC_WECHAT, UA_ANDROID_WECHAT, UA_IOS_SAFARI, UA_WINDOWS_CHROME):
        assert resolve_payment_channel("auto", ua) == detect_payment_channel(ua)


def test_no_second_channel_implementation_anywhere() -> None:
    """下单入口必须共用同一份判据 —— 少一个,桌面微信就从那个口子漏过去。

    2026-08-02 教训:两处各写一份同类修复,删哪个哪边红,一份工白写。
    [开源 E3 · B4 · 2026-09-28] 原第三个入口(订阅支付模块)随订阅产品面整文件删除,剩在役的两个。
    """
    entrypoints = ["wallet_api.py", "agent_workbench_api.py"]
    for name in entrypoints:
        path = REPO / "api" / name
        src = path.read_text(encoding="utf-8")
        assert "detect_payment_channel" in src, f"{name} 必须委托 SSOT"
        # 必须不命中:不许在 API 层自己再拼一遍 UA 判据。
        # 🔴 判据打在 **AST 里的真字符串常量**上,不是整份文本 —— 这三份的 docstring 里
        #    都写着"UA 含 MicroMessenger"这类说明,按文本查会被自己的文档骗成红。
        for literal in _py_string_literals(path):
            assert "micromessenger" not in literal.lower(), \
                f"{name} 里出现了第二份 UA 判据: {literal[:80]!r}"
    # 反向对照:同一个判据在 SSOT 上必须命中(否则它就是恒真的)
    assert any("micromessenger" in s.lower()
               for s in _py_string_literals(REPO / "services" / "payment_routing.py")), \
        "SSOT 里反而找不到 UA 字面量 → 这条判据没有判别力"


# ════════════════════════════════════════════════════════════════════════════
# 跨语言 token 对齐(前端 paymentEnv.ts 是 payment_routing.py 的镜像)
# ════════════════════════════════════════════════════════════════════════════

def _ts_token_list(src: str, const_name: str) -> list[str]:
    m = re.search(rf"{const_name}\s*=\s*\[(.*?)\]", src, re.S)
    assert m, f"paymentEnv.ts 里找不到 {const_name}"
    return re.findall(r"'([^']+)'", m.group(1))


def test_frontend_ua_tokens_mirror_the_backend_ssot() -> None:
    """改一边不改另一边 → 后端按 PC 发二维码、前端按微信内渲文案,界面自相矛盾。"""
    src = _code(PAY_ENV_TS)
    assert _ts_token_list(src, "DESKTOP_WECHAT_UA_TOKENS") == list(DESKTOP_WECHAT_UA_TOKENS)
    assert _ts_token_list(src, "MOBILE_UA_TOKENS") == list(MOBILE_UA_TOKENS)


def test_frontend_desktop_wechat_is_excluded_from_mobile() -> None:
    """前端 isMobileUa 必须先把桌面微信摘出去(它 UA 里没有 mobile,但别人可能加 token)。"""
    src = _code(PAY_ENV_TS)
    m = re.search(r"export function isMobileUa\([^)]*\)[^{]*\{(.*?)\n\}", src, re.S)
    assert m, "找不到 isMobileUa 实现"
    assert "isDesktopWechatUa" in m.group(1)


# ════════════════════════════════════════════════════════════════════════════
# §2.1 三个埋点 + §2.3 移动端同窗跳(前端源码级判据)
# ════════════════════════════════════════════════════════════════════════════

def test_three_pay_events_exist_and_are_fire_and_forget() -> None:
    src = _code(PAY_TELEMETRY_TS)
    for ev in ("pay_dialog_shown", "pay_link_clicked", "pay_jump_left_page"):
        assert f"'{ev}'" in src, f"缺埋点 {ev}"
    # 工单 §3:埋点失败不得阻塞支付主链
    assert src.count("try {") >= 3, "三个 track 函数都要包 try/catch"
    assert "await" not in src, "埋点不许 await(会把支付按钮拖住)"


def test_click_and_leave_events_flush_immediately() -> None:
    """🔴 跳走之后队列就没机会 flush 了 —— clicked / left 必须走 trackEventNow。"""
    src = _code(PAY_TELEMETRY_TS)
    clicked = re.search(r"export function trackPayLinkClicked[\s\S]*?\n\}", src)
    left = re.search(r"export function trackPayJumpLeftPage[\s\S]*?\n\}", src)
    assert clicked and "trackEventNow" in clicked.group(0)
    assert left and "trackEventNow" in left.group(0)
    # 必须不命中:shown 不需要立刻发(它发生在用户还在页上的时候),用普通 trackEvent
    shown = re.search(r"export function trackPayDialogShown[\s\S]*?\n\}", src)
    assert shown and "trackEventNow" not in shown.group(0)


def test_leave_tracking_uses_pagehide_not_only_beforeunload() -> None:
    """iOS Safari 上 beforeunload 常常不触发 —— 只听它等于没听。

    🔴 判据必须打在**监听注册**上,不能只查字符串 ``'pagehide'`` 有没有出现:
    该字面量在 ``fire('pagehide')`` 的 reason 里也有一份,查字符串会被自己的另一处骗过去
    (2026-08-07 变异 M13 实测存活,就是这么活下来的)。
    """
    src = _code(PAY_TELEMETRY_TS)
    assert "addEventListener('pagehide'" in src, "没注册 pagehide 监听"
    assert "addEventListener('visibilitychange'" in src, "没注册 visibilitychange 监听"
    # 必须不命中:beforeunload 在 iOS 上不可靠,不许拿它当离页信号
    assert "addEventListener('beforeunload'" not in src
    # 装了就要拆(弹窗关掉后不该继续监听)
    assert "removeEventListener('pagehide'" in src
    assert "removeEventListener('visibilitychange'" in src


def test_analytics_flush_checks_token_before_dropping_the_batch() -> None:
    """原写法先 splice 再判 token,拿不到 token 时整批被静默吞掉。"""
    src = _code(ANALYTICS_TS)
    m = re.search(r"function flush\(\)[\s\S]*?\n\}", src)
    assert m, "找不到 flush()"
    body = m.group(0)
    assert body.index("getConfirmedSessionToken") < body.index("EVENTS_QUEUE.splice"), \
        "token 判断必须排在 splice 之前"
    assert "flushAnalyticsNow" in src and "trackEventNow" in src


def test_pay_dialogs_have_no_unconditional_blank_target() -> None:
    """§2.3 必须不命中:支付链接不许再写死 target="_blank"。

    iOS Safari「阻止弹出式窗口」会把它静默拦掉 —— 不报错、不进 catch,
    用户看到的就是"点了没反应"。

    [#169 换锚] 分母 = 三个支付面的**并集**(出口已收进 PayExit.tsx)。
    """
    combined = "\n".join(_code(p) for p in PAY_SURFACES)
    anchors = [m.group(0) for m in re.finditer(r"<a\b[^>]*>", combined, re.S)
               if "payment_url_mobile" in m.group(0)]
    # 🔴 反向对照:找不到支付 anchor 时下面的循环会**空转即通过** —— 那比漏检更坏。
    assert anchors, "三个支付面里一个支付 anchor 都没找到,判据失效了"
    for tag in anchors:
        assert 'target="_blank"' not in tag, f"支付链接仍写死 _blank: {tag[:120]}"
    # 合规形态只有两种:走统一判据(桌面才开新窗)· 或压根不带 target(微信内那条,§3 明令不动)
    assert "payLinkTargetProps()" in _code(PAYEXIT_TSX), "支付出口没有走统一 target 判据"


@pytest.mark.parametrize("path", [INVENTORY_TSX, BUYCREDIT_TSX])
def test_pay_dialogs_are_wired_to_all_three_events(path: Path) -> None:
    """页面这一半:每个支付面自己报 shown、自己装离页监听。"""
    src = _code(path)
    assert "trackPayDialogShown(" in src, f"{path.name} 不报 pay_dialog_shown"
    assert "usePayJumpTracking(" in src, f"{path.name} 没装离页监听"
    # 🔴 **那条边**:页面必须把 arm 传进出口组件。原版没钉它 ——
    #    页面装了监听、出口点了链接,但两者没接上时 left 事件永远不发,
    #    而两边**各自**看都是「接线齐全的」。
    assert "armJumpTracking={armJumpTracking}" in src, \
        f"{path.name} 没把 armJumpTracking 传给 PayExit,left 事件永远不发"


def test_pay_exit_wires_click_and_arm() -> None:
    """出口这一半:点击时必须发 clicked 并 arm,否则 left 永远不发。"""
    src = _code(PAYEXIT_TSX)
    assert "trackPayLinkClicked(" in src, "支付出口不报 pay_link_clicked"
    assert "armJumpTracking()" in src, "点击时必须 arm,否则 left 事件永远不发"


@pytest.mark.parametrize("path", [INVENTORY_TSX, BUYCREDIT_TSX])
def test_pay_dialog_is_scrollable_on_short_viewports(path: Path) -> None:
    """§2.4 支付弹窗移动视口可点性。

    Radix DialogContent 是 ``fixed top-50% -translate-y-1/2`` 且**没有 max-height**
    (components/ui/dialog.tsx 现役类名里既无 max-h 也无 overflow)——
    内容高过视口时上下溢出屏幕**且滚不动**,支付按钮会落在够不着的地方。
    """
    src = _code(path)
    m = re.search(r"<DialogContent[^>]*>", src, re.S)
    assert m, f"{path.name} 找不到 DialogContent"
    tag = m.group(0)
    assert "max-h-[calc(100dvh" in tag, f"{path.name} 支付弹窗没设视口高度上限"
    assert "overflow-y-auto" in tag, f"{path.name} 支付弹窗溢出后滚不动"


def test_wechat_inline_auto_redirect_form_is_untouched() -> None:
    """🔴 工单 §3 必须不命中:不许动 C 端微信内自动跳。

    ``location.href`` + sessionStorage 防循环已是正确形态。
    """
    src = _code(BUYCREDIT_TSX)
    m = re.search(r"xunhupay_jumped_[\s\S]{0,900}?\}, 1000\);", src)
    assert m, "微信内 1 秒自动跳那段不见了"
    seg = m.group(0)
    assert "sessionStorage.setItem" in seg, "防循环标记被删了"
    assert "window.location.href" in seg, "自动跳被改成了别的形态"
    assert "window.open" not in seg, "微信内不许改成 window.open(会被拦)"
