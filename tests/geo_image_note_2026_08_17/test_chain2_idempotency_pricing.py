"""返工链 2 · 幂等根 + 逐项价格锁 + extra 进冻结(P0-02 / P1-3)。

每条"必须命中"都配一条"必须不命中" —— 上一轮四次自伤全是配对判据抓住的,
这条纪律现在是本包的默认写法,不是补丁。
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
API = REPO / "api" / "geo_image_note_api.py"
FREEZE = REPO / "services" / "geo_douyin" / "contract_freeze.py"


def _code_only(path: pathlib.Path) -> str:
    """抹注释与 docstring,其余逐字保留(与 test_real_http_contract 同一实现口径)。"""
    import tokenize

    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return "\n".join(lines)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            for ln in range(first.lineno - 1, (first.end_lineno or first.lineno)):
                if 0 <= ln < len(lines):
                    lines[ln] = ""
    try:
        with io.open(path, encoding="utf-8") as fh:
            for tok in tokenize.generate_tokens(fh.readline):
                if tok.type == tokenize.COMMENT:
                    ln = tok.start[0] - 1
                    if 0 <= ln < len(lines):
                        lines[ln] = lines[ln][:tok.start[1]]
    except tokenize.TokenError:
        pass
    return "\n".join(lines)


def _create_batch_body() -> str:
    fn = next(n for n in ast.walk(ast.parse(_code_only(API)))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_create_batch")
    return ast.unparse(fn)


# ---------------------------------------------------------------------------
# 幂等根真的接上了
# ---------------------------------------------------------------------------

def test_create_batch_calls_claim_request():
    """🔴 P0-02:原来 handler **根本没调** claim_request,每次自造 uuid。

    WP2 建的原子幂等是真的(ON CONFLICT DO NOTHING + 三者比对 + 20 并发判据),
    只是这条链从没把它接上 —— 「模块真实但执行链没接通」的标本。
    """
    body = _create_batch_body()
    assert "claim_request(" in body, "create-batch 没有接幂等根"
    assert "request_id=req.request_id" in body, "claim 用的不是前端传来的幂等键"


def test_claim_result_is_actually_used_not_just_present():
    """🔴 变异实测抓到的洞:我的判据原来只查 `claim_request(` **这段文字在不在**,
    而文字可以被架空 —— `is_new, claimed = (True, {}) or claim_request(...)`
    保留了全部文本,判据全绿,幂等根却已经废掉。

    这正是本包被撤销的那个病的缩小版:**锁的是文本不是接线**。
    改成打 AST:`is_new` 的赋值右侧必须**直接**是 claim_request 调用。
    """
    fn = next(n for n in ast.walk(ast.parse(_code_only(API)))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_create_batch")
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Tuple)
                       and any(getattr(e, "id", None) == "is_new" for e in t.elts)
                       for t in n.targets)]
    assert assigns, "找不到 is_new 的赋值 —— 幂等根没接"
    value = assigns[0].value
    assert isinstance(value, ast.Call), (
        f"is_new 的右侧是 {type(value).__name__} 而不是直接调用 —— "
        "幂等根被表达式架空了")
    assert getattr(value.func, "id", None) == "claim_request", (
        "is_new 不是由 claim_request 直接产出")


def test_claim_happens_before_any_write():
    """claim 必须在**建批次之前**。放在后面等于先产生副作用再问该不该做。"""
    body = _create_batch_body()
    claim_at = body.index("claim_request(")
    insert_at = body.index("INSERT INTO geo_douyin_production_batches")
    assert claim_at < insert_at, "claim 跑在建批次之后,副作用先于幂等判定"


def test_replay_path_produces_no_new_side_effects():
    """`is_new=False` 时只回放,不再往下走建单/冻结。"""
    body = _create_batch_body()
    assert "if not is_new:" in body
    replay_at = body.index("if not is_new:")
    insert_at = body.index("INSERT INTO geo_douyin_production_batches")
    assert replay_at < insert_at, "回放分支没有在建单之前返回"


def test_no_random_uuid_as_the_idempotency_key():
    """反向对照:批次 id 仍可以是 uuid(它是主键),但**幂等键**不许是它。"""
    body = _create_batch_body()
    assert "req.request_id," in body, "落库的 request_id 不是前端那把钥匙"


# ---------------------------------------------------------------------------
# 逐项重算与确认锁
# ---------------------------------------------------------------------------

def test_no_total_divided_by_item_count():
    """🔴 P0-02:原来 `expected_total // len(items)` 把总价摊平。

    卡数不同的两项价格本就不同,摊平之后**每一笔冻结都是错的**;
    而且它信的是客户端报的总价,服务端从未自己算过。
    """
    body = _create_batch_body()
    assert "// max(1, len(item_dicts))" not in body, "总价仍在按项数摊平"
    assert "_recompute_items(" in body, "没有服务端重算"


def test_per_item_fingerprint_is_verified():
    body = _create_batch_body()
    assert "assert_price_unchanged(" in body, "逐项指纹没有确认锁"
    assert "OPERATION_PRODUCTION" in body, "确认锁没有绑定制作链(跨链传入会漏)"


def test_total_confirmation_lock_exists():
    """总价确认锁:服务端重算总价与用户确认时的不一致 → 零冻结零创建。"""
    body = _create_batch_body()
    assert "server_total" in body and "PriceChanged" in body


def test_freeze_uses_recomputed_price_not_client_total():
    body = _create_batch_body()
    assert "expected_points=int(_calc['final_price_points'])" in body \
        or 'expected_points=int(_calc["final_price_points"])' in body, \
        "冻结用的不是服务端重算价"


def test_recompute_reuses_the_preview_pricing_source():
    """🔴 重算必须复用预览的定价源,不许再写一份"差不多的算法"。

    两份算法必然漂移,而漂移那天的表现是"预览和扣费对不上" ——
    要翻两处实现才查得出。
    """
    src = _code_only(API)
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_recompute_items")
    body = ast.unparse(fn)
    for name in ("read_unit_points", "extra_card_points", "production_fingerprint"):
        assert name in body, f"重算没有复用 {name},定价出现第二份实现"
    # 反向对照:不许出现自造的算术常量(那就是第二份算法的样子)
    assert "390" not in body and "* 1.0" not in body


# ---------------------------------------------------------------------------
# extra_cost 进冻结
# ---------------------------------------------------------------------------

def test_freeze_adapter_accepts_extra_cost():
    """🔴 P0-02:`contract_freeze` 原来写死 `base_pricing_extra = 0`。

    于是**多卡加价从不进冻结额**:预览算了、按钮显示了、却没冻住,
    差额到结算才暴露,而那时用户早已离开。
    """
    src = _code_only(FREEZE)
    assert "base_pricing_extra = 0" not in src, "加价仍被写死成 0"
    assert "extra_cost: int = 0" in src, "冻结适配器没有接收 extra_cost 的入口"
    assert "extra_cost=int(extra_cost)" in src, "参数收了却没往下传"


def test_batch_passes_extra_cost_to_freeze():
    body = _create_batch_body()
    assert "extra_cost=int(_calc['extra_points'])" in body \
        or 'extra_cost=int(_calc["extra_points"])' in body, \
        "批量创建没有把加价传进冻结"


# ---------------------------------------------------------------------------
# 真行为:重算函数自己要对
# ---------------------------------------------------------------------------

def test_recompute_charges_more_for_more_cards(monkeypatch):
    """行为判据:卡数多的那一项价必须更高。

    只打"调用了哪些函数"的结构判据挡不住"参数传错" ——
    比如把 card_count 写死成默认值,结构判据全绿而每一项都算成一样的价。
    """
    import api.geo_image_note_api as mod

    async def _unit(_code):
        return 390

    async def _extra(card_count):
        return max(0, int(card_count) - 4) * 30

    monkeypatch.setattr("services.geo_douyin.pricing.read_unit_points", _unit)
    monkeypatch.setattr("services.geo_douyin.pricing.extra_card_points", _extra)

    out = mod._recompute_items([
        {"settings": {"card_count": 4}},
        {"settings": {"card_count": 7}},
    ])
    assert out[0]["final_price_points"] == 390
    assert out[1]["final_price_points"] == 480          # 390 + 3×30
    assert out[1]["extra_points"] == 90
    # 反向对照:两项指纹必须不同,否则确认锁形同虚设
    assert out[0]["fingerprint"] != out[1]["fingerprint"]


def test_recompute_zero_cards_does_not_crash(monkeypatch):
    """边界:0 卡不该炸,也不该变成默认 4 卡(那是又一次"显式 0 被吃掉")。"""
    import api.geo_image_note_api as mod

    async def _unit(_code):
        return 390

    async def _extra(card_count):
        return max(0, int(card_count) - 4) * 30

    monkeypatch.setattr("services.geo_douyin.pricing.read_unit_points", _unit)
    monkeypatch.setattr("services.geo_douyin.pricing.extra_card_points", _extra)
    out = mod._recompute_items([{"settings": {"card_count": 0}}])
    assert out[0]["extra_points"] == 0


# ---------------------------------------------------------------------------
# 契约有两道:DTO 放行 ≠ 库放行
# ---------------------------------------------------------------------------

# ── 已退役（Review #161）· 规格 01 §10/§3.1/§5 该条款由 Owner #150 作废 ──────────
# test_idempotency_key_is_a_real_uuid：UUID 列属已撤 post_tasks；新 geo_douyin_post_batches.request_id 为 TEXT，前端不再生成 uuid 是正确的



def test_uuid_shape_probe_discriminates():
    """反向对照:形状检查要真的能分辨。手工造两个串验一次。"""
    import re as _re

    good = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    bad = "batch-mabc12-xyz"
    pat = _re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    assert pat.match(good)
    assert not pat.match(bad)
