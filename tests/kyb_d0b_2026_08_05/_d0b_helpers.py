"""[WO-KYB D0-b] 测试辅助 —— 模块名唯一。

不放 conftest 是因为多个测试包的 conftest 同名,
各自把自己目录塞进 sys.path 后 `from conftest import x` 会解析到别人那份,
几个套件合起来跑就 ImportError(Review 正是合起来跑的)。
"""


def media_row(**kw):
    """一行目录数据的最小形状(成本侧字段齐全,用来验它们真被删干净)。"""
    row = {
        "id": 1, "media_name": "某某网",
        "price": 40.0, "price1": 45.0, "price2": 50.0,
        "price_normal": 40.0, "price_vip": 38.0, "price_svip": 36.0,
        "video_price": 100.0, "weitoutiao_price": 20.0,
        "hepai_price": 60.0, "hepai_price1": 61.0, "hepai_price2": 62.0,
        "our_price_yuan": None, "our_price_points": None,
        "geo_rank_platform": "doubao,kimi,deepseek,qwen",
    }
    row.update(kw)
    return row
