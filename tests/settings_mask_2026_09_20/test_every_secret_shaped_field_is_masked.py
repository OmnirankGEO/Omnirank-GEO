# -*- coding: utf-8 -*-
"""WO_248 · `GET /api/settings` 里**每一个**密钥形态的字段都必须遮蔽。

事实(a4 发现 · Deploy 只读实证 · Review 读码确认):
`load_settings()` 把 env 值并进模型,而 `get_settings_for_frontend()`
原来只对 **8 个**字段调 `mask_api_key`,漏了 `doubao_endpoint_id` /
`jina_api_key` / `kuaiyibo_api_token` / `publish_channel_callback_secret` ——
生产 env 里前三者非空 ⇒ **原样回给管理员浏览器**。

🔴 本包的重点不是"补上这四个",是**防第 13 个**:
   靠「记得加」守不住 —— 漏这四个的人当初也"记得加"了八个。
   所以按**字段名形态**枚举整个模型,凡长得像密钥的都必须遮。
   新增字段时忘了遮,这一格当场红,而不是等下一次安全梳理。

🔴 「只有平台管理员看得到」不是不遮的理由:遮蔽防的是浏览器缓存、截图、录屏、
   F12、以及任何把响应体带出这台机器的动作。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: 长得像密钥的字段名形态。**后缀匹配**,避免把 `api_key_updated_at` 这类误判。
SECRET_SHAPE = re.compile(r"(api_key|token|secret|password|endpoint_id)$")

#: 本单新补的四个 —— 单独点名,便于反向对照逐个下毒。
NEWLY_MASKED = (
    "doubao_endpoint_id",
    "jina_api_key",
    "kuaiyibo_api_token",
    "publish_channel_callback_secret",
)


def _model_fields():
    from config.settings_manager import SystemSettings
    fields = getattr(SystemSettings, "model_fields", None)
    if fields is None:                       # pydantic v1 兜底
        fields = getattr(SystemSettings, "__fields__", {})
    return list(fields.keys())


def test_the_instrument_actually_sees_the_model():
    """🔴 仪器自检:字段列表不能是空的,否则下面每一条都会"全绿"。"""
    names = _model_fields()
    assert len(names) >= 10, "只枚举到 %d 个字段 —— 解析坏了,不是都遮了" % len(names)
    shaped = [n for n in names if SECRET_SHAPE.search(n)]
    assert len(shaped) >= 12, (
        "密钥形态字段只数到 %d 个 —— 形态正则或模型变了,判据失去分母:%s"
        % (len(shaped), shaped))


def _frontend_view(monkeypatch, value="SECRET-VALUE-1234567890"):
    """给每个密钥形态字段塞一个可识别的值,再看前端视图里还剩什么。"""
    import config.settings_manager as sm
    real = sm.load_settings()
    stuffed = real.model_copy(deep=True) if hasattr(real, "model_copy") else real.copy(deep=True)
    for name in _model_fields():
        if SECRET_SHAPE.search(name):
            try:
                setattr(stuffed, name, value)
            except Exception:                # 类型不是 str 的就跳过(它本来也不是密钥)
                pass
    monkeypatch.setattr(sm, "load_settings", lambda: stuffed)
    return sm.get_settings_for_frontend(), value


def test_every_secret_shaped_field_is_masked(monkeypatch):
    """🔴 类锁:凡字段名以 api_key/token/secret/password/endpoint_id 结尾的,
    前端视图里**不许出现原值**。

    这一条管的是**下一个**新增字段,不是已知这几个。
    """
    view, value = _frontend_view(monkeypatch)
    leaked = [
        name for name in _model_fields()
        if SECRET_SHAPE.search(name) and view.get(name) == value
    ]
    assert not leaked, "这些密钥形态字段原样回给了前端:%s" % (leaked,)


@pytest.mark.parametrize("name", NEWLY_MASKED)
def test_each_newly_masked_field_is_covered(name, monkeypatch):
    """🔴 反向对照一:本单新补的四个逐个点名 —— 去掉任一个,这一格必红。"""
    view, value = _frontend_view(monkeypatch)
    assert name in view, "%s 不在前端视图里 —— 判据失去参照物" % name
    assert view[name] != value, "%s 原样回给了前端" % name


def test_a_synthetic_new_secret_field_would_be_caught(monkeypatch):
    """🔴 反向对照二(工单点名):造一个**模型里没有的**密钥形态字段,
    如果它没被遮,类锁必须能发现。

    这一条验的是**类锁本身有没有分辨力** —— 没有它,上面那条全绿
    可能只是因为"所有字段恰好都被遮了"而不是因为锁在工作。
    """
    import config.settings_manager as sm
    real = sm.load_settings()
    stuffed = real.model_copy(deep=True) if hasattr(real, "model_copy") else real.copy(deep=True)
    monkeypatch.setattr(sm, "load_settings", lambda: stuffed)

    value = "SYNTHETIC-LEAK-0987654321"
    original = sm.get_settings_for_frontend

    def _with_extra_field():
        data = original()
        data["x_api_key"] = value            # 模型外新增,没人遮它
        return data

    monkeypatch.setattr(sm, "get_settings_for_frontend", _with_extra_field)
    view = sm.get_settings_for_frontend()
    leaked = [k for k, v in view.items() if SECRET_SHAPE.search(k) and v == value]
    assert leaked == ["x_api_key"], (
        "人造的未遮字段没有被这套形态判别抓到(leaked=%s)—— 类锁没有分辨力" % (leaked,))


def test_the_confirm_code_hashes_are_still_removed(monkeypatch):
    """🔴 既有行为不许被顺手改掉:确认码哈希仍然整块删除。"""
    view, _ = _frontend_view(monkeypatch)
    assert "confirm_codes" not in view, "confirm_codes 又回到前端视图里了"
