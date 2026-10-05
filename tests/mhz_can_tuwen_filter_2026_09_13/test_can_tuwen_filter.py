"""#192 c1 判据 —— 账号列表按「能发图文」服务端过滤。

现场:面板取 `GET /api/meijiehezi/short-video?limit=50&page=1`,那是媒介盒子
**供应商目录**(生产两万余条,第一页 50 条 can_tuwen 全 0),接口又没有这个过滤参数
⇒ 前端在第一页里筛永远筛不到 ⇒ 显示成「这个客户名下还没有能发图文的账号」。
"""
from .conftest import seed_media


def _list(**kw):
    from db.meijiehezi_db import list_short_video
    return list_short_video(1, 50, **kw)


def test_c1_filter_returns_only_image_note_capable_accounts():
    for i, cap in ((1, 1), (2, 0), (3, 1), (4, 0), (5, 0)):
        seed_media(i, can_tuwen=cap)

    out = _list(can_tuwen=1)

    # 键名是 `media`(db/meijiehezi_db.py:1639)—— 读出来的,不是猜的。
    # 我第一版猜了 items/list/data 三个都不对,断言当场落空;
    # 幸好它是 `assert rows` 而不是 `for r in rows or []`,否则会静默变成空循环恒绿。
    rows = out["media"]
    assert rows, "返回结构变了 —— 先确认键名,别让断言落空"
    assert all(int(r["can_tuwen"]) == 1 for r in rows), rows
    assert int(out["total"]) == 2, "total 必须是**过滤后**的数,否则翻页会翻出空页"


def test_c1_no_parameter_is_unchanged():
    """🔴 反向对照:不带参数 = 老调用,逐字节同行为。

    少了这条,把过滤写成"永远只给 can_tuwen=1"也能让上面那条绿,
    而那会把所有只发视频的账号从整个媒体库里抹掉。
    """
    for i, cap in ((1, 1), (2, 0), (3, 0)):
        seed_media(i, can_tuwen=cap)

    out = _list()
    assert int(out["total"]) == 3, "不带参数时被过滤了 —— 老调用行为变了"


def test_c1_zero_selects_the_other_side():
    """can_tuwen=0 精确匹配另一侧 —— 证明它是**参数**不是硬编码的 1。"""
    for i, cap in ((1, 1), (2, 0), (3, 0)):
        seed_media(i, can_tuwen=cap)

    out = _list(can_tuwen=0)
    assert int(out["total"]) == 2
