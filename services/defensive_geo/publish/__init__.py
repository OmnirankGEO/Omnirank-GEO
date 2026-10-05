"""防御型 GEO · WP5(一问一稿一媒体)+ WP6(发布、资金与恢复)域层。

规格 §11 / §12 / §15.7 / §18.4 / §18.5 @ spec e710be6c2。

本包的分层铁律
--------------
1. **纯函数层不碰库、不碰钱**(`media_identity` / `body_hash` / `publish_slot` /
   `decision_snapshot` / `publish_settlement` / `legal_gate` / `settlement_review`):
   它们只回答「这个投影自洽吗 / 这一格该往哪走」。判据拿它们当分母逐格核对。
2. **落库层只有 `store`**,资金层只有 `publish_funding`。
   `publish_funding` 是**全包唯一** import 计费原语的模块 —— 结构锚打在这一条上,
   别处出现 `middleware.billing` 的 import 就是把钱腿开了第二个出口。
3. **六个**受保护文件零 diff(`middleware/billing.py`、`db/wallet_db.py`、
   `db/connection.py`、`auth/middleware.py`、`auth/jwt_utils.py`、
   `config/settings_manager.py`):只调它们的公共接口,一行都不改(§12.4)。

   🔴 [R1 订正] 这里原来写「七保护文件」却只列了六个 —— 数字与名单打架。
      第七个从来不是它们的同类:`db/migration_manifest.py` **必须**被改
      (不登记 = 迁移永远不会跑),它走的是另一条口径 ——
      **只允许追加一行 + 注释**,单独核验,不进"零 diff"名单。
      另注:部署工具箱 `.deploy_toolkit/preflight.sh` 自己那张七件套是**另一张**
      名单(多 `config/pricing_config.py` / `tools/transparent_pricing.py`,
      少 `config/settings_manager.py`)。两张都不许被本包改,但别把它们混成一张。
"""

__all__ = [
    "action_registry",
    "body_hash",
    "decision_snapshot",
    "legal_gate",
    "media_identity",
    "media_proposal",
    "publish_funding",
    "publish_outbox",
    "publish_settlement",
    "publish_slot",
    "reconciler",
    "service_milestone",
    "settlement_alerts",
    "settlement_review",
    "store",
]


def census() -> dict:
    """全包机械分母的**汇总出口**(§0.5.3 G-2)。

    判据、端点 ``/contract-census``、以及日后 WP8 的全仓 registry 合并
    都从这里取,不各自 import 一堆模块再手拼 —— 手拼的那份必然漏。
    """
    from importlib import import_module

    out: dict[str, dict] = {}
    for name in __all__:
        mod = import_module(f"{__name__}.{name}")
        fn = getattr(mod, "census", None)
        if callable(fn):
            out[name] = fn()
    return out
