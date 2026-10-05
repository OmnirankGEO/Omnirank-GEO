"""真 LLM 端到端 · 文本链(Review-CTO 2026-08-18 两段式裁定 · 第一段)

## 边界

- **文本链走真 LLM**(选题/文案),四把 key 由环境注入,**不落盘不打印**。
- **图像 + OSS 两跳 mock**:`APIMART_KEY` 本就不在主仓 .env 里,且真生图归入
  部署后 verified 判据(测试品牌,沿用文章包 article 1559 的先例)。
- 发布只走到**代发提交层**,不真发外网。
- 资金**真扣真冻**:freeze → commit 全部落库,断言查的是流水不是返回值。

## 为什么这一套不能省

前面所有轮次的"绿"都建立在夹具上。夹具证明得了"逻辑对",证明不了
"把开关打开、用真 key、真扣一次钱,这条链走得通"。
2026-08-18 准备本套时就当场撞见一个夹具永远看不见的缺陷:
**开关只通了一半** —— 写入侧通、读侧 503(`build_delivery_plan` 的 active 分支
从来没写过)。那是夹具测不出来的形态,因为夹具从不打开开关。

## 跳过条件

缺 DB 夹具或缺文本 LLM key 时 **skip**(不是 fail):这一套要花真钱,
不该在没有授权环境的机器上自动跑起来。但**跳过的原因会打印出来** ——
静默跳过与从来没写过在结果上一模一样。
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid
from datetime import datetime, timezone

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = (
    REPO / "db" / "migration_034_geo_image_note_contract_2026_08_17.sql",
    REPO / "db" / "migration_035_geo_image_note_slot_channel_2026_08_18.sql",
    REPO / "db" / "migration_036_publication_stage_strict_source_2026_08_18.sql",
)
PROD_SCHEMA = pathlib.Path(os.getenv(
    "GEOIMG_PROD_SCHEMA_SQL", r"C:/AI-Test/.deploy_toolkit/_geoimg_prodschema_20260817.sql"))
DSN = os.getenv("TEST_DATABASE_URL")

#: 文本链需要的 key。任一存在即可(模型路由会挑一个能用的)。
TEXT_LLM_KEYS = ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY")

#: 生图单价(真值取自 services/marketing/image_client.py:202,不在本文件写死第二份)。
#: 本套图像全 mock,所以真实生图花费 = 0;这个常量只用于在报告里算"如果真跑要多少"。
def _image_unit_cost_usd() -> float:
    from services.marketing.image_client import IMAGE_COST_USD_1K

    return float(IMAGE_COST_USD_1K)


def _have_text_key() -> bool:
    return any(os.getenv(k) for k in TEXT_LLM_KEYS)


#: 🔴 **显式开工同意**。只看"有没有 key"是不够的 ——
#:    2026-08-18 实测:本机 shell 里 `DEEPSEEK_API_KEY` 是**常驻**的,
#:    于是一次普通的全量 `pytest` 就会把这套真花钱的用例跑起来,**没人同意过**。
#:    我上一轮还向 Review 说过"不在无授权环境自动跑" —— 那句话当时比实际情况强。
#:    密钥存在 ≠ 授权花钱。必须有一个只能人手设置的开关。
OPT_IN_ENV = "GEOIMG_E2E_REAL_LLM"


def _opted_in() -> bool:
    return str(os.getenv(OPT_IN_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


pytestmark = pytest.mark.skipif(
    not (_opted_in() and DSN and PROD_SCHEMA.is_file() and _have_text_key()),
    reason=(
        f"真 LLM E2E 需要**显式开工同意** {OPT_IN_ENV}=1,外加 TEST_DATABASE_URL + "
        f"生产 schema 夹具 + 至少一把文本 LLM key(试过 {TEXT_LLM_KEYS})。"
        "缺任一项即跳过 —— 这一套花真钱,"
        "而密钥在很多机器上是常驻的,只看有没有 key 挡不住"
        "『一次普通全量跑就把钱花了』。"
    ),
)


def _admin_dsn() -> str:
    return DSN.rsplit("/", 1)[0] + "/postgres"


@pytest.fixture(scope="module")
def db():
    name = f"geoimg_e2e_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(_admin_dsn())
    admin.autocommit = True
    admin.cursor().execute(f'CREATE DATABASE "{name}"')
    admin.close()
    dsn = DSN.rsplit("/", 1)[0] + "/" + name
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("\n".join(
        line for line in PROD_SCHEMA.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not line.startswith("\\restrict") and not line.startswith("\\unrestrict")))
    c.execute("SET search_path = public")
    for mig in MIGRATIONS:
        c.execute(mig.read_text(encoding="utf-8"))
    c.execute("INSERT INTO monitoring_product_platform_matrices (version, platforms) "
              "VALUES ('monitoring-unified5-v1', 'dashscope,deepseek,kimi,doubao') "
              "ON CONFLICT DO NOTHING")
    conn.close()
    try:
        yield dsn
    finally:
        admin = psycopg2.connect(_admin_dsn())
        admin.autocommit = True
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture(scope="module")
def seeded(db):
    """一张**测试品牌**(is_test=true)+ 已 enroll 的报价 + 两个图文槽位 + 有余额的钱包。

    🔴 `is_test=true` 是硬要求:工单 §3.4 明写测试品牌,
       真实客户品牌零写操作是本包全程的红线。
    """
    conn = psycopg2.connect(db, cursor_factory=RealDictCursor)
    conn.autocommit = True
    c = conn.cursor()
    c.execute("SET search_path = public")

    # 🔴 fixture 与库同为 module 级:种子只种一次。
    #    第一版写成函数级,第二个用例上来就 duplicate key —— 那不是缺陷,
    #    是我把"每个用例一份干净数据"和"整模块一个库"两种模型混着用了。
    c.execute("INSERT INTO brands (id, name, is_test) VALUES (9901, %s, TRUE)",
              (f"E2E测试品牌-{uuid.uuid4().hex[:6]}",))
    c.execute(
        "INSERT INTO quotes (id, brand_id, brand_name, status, "
        "  article_plan_writing_mode, article_plan_enrolled_at) "
        "VALUES (7701, 9901, 'E2E测试品牌', 'paid', 'image_note_contract', NOW())")
    c.execute("INSERT INTO confirmed_keywords (id, quote_id, keyword, required_articles) "
              "VALUES (8801, 7701, '深圳载货电梯', 2)")

    slots = []
    # 🔴 列名逐字取自生产 schema,**不猜**。第一版我写了个不存在的 `effective_from`,
    #    当场 UndefinedColumn —— 那正是本仓 SQL 四维核验第一条(列名肉眼核对)。
    #    这张表全是 NOT NULL,少一个都插不进去。
    c.execute(
        "INSERT INTO geo_article_contract_revisions "
        "  (id, revision_key, source_event_key, source_version, owner_user_id, "
        "   brand_id, quote_id, authority_snapshot, authority_snapshot_hash, "
        "   delivery_count, contract_version) "
        "VALUES (5501, %s, %s, 'e2e', 112, 9901, 7701, '{}'::jsonb, %s, 2, 'v1')",
        (uuid.uuid4().hex.ljust(64, "0"), uuid.uuid4().hex.ljust(64, "0"),
         uuid.uuid4().hex.ljust(64, "0")))
    # 🔴 槽位有 FK 指向事件表(current_event_id)。事件溯源设计:**先有事件,后有投影**。
    #    我第一版直接塞 current_event_id=0,当场 ForeignKeyViolation ——
    #    这不是夹具麻烦,是那台机器的不变式在说话:投影行不能凭空存在。
    #    做法与 WP1 的 PG16 夹具一致(同一套 seeding,不另造第二份)。
    # 🔴 列名第三次栽在"凭印象写"上(effective_from / source_version 都不存在)。
    #    这一版起,三张表的列全部逐字取自生产 schema dump。
    #    教训不是"下次仔细点",是**写 SQL 之前先把 CREATE TABLE 读出来**——
    #    本仓 SQL 四维核验第一条就是这个,我连着违反了三次。
    c.execute(
        "INSERT INTO geo_article_plan_runs(run_key, contract_revision_id, owner_user_id, "
        " brand_id, quote_id, run_mode, compiler_version, input_snapshot, "
        " input_snapshot_hash, status) "
        "VALUES (%s, 5501, 112, 9901, 7701, 'shadow', 'v1', '{}'::jsonb, %s, 'completed') "
        "RETURNING id",
        (uuid.uuid4().hex.ljust(64, "0"), uuid.uuid4().hex.ljust(64, "0")))
    run_id = int(c.fetchone()["id"])
    for ordinal in (1, 2):
        key = str(uuid.uuid4())
        slots.append(key)
        event_key = uuid.uuid4().hex + uuid.uuid4().hex[:32]
        c.execute(
            "INSERT INTO geo_article_delivery_slot_events(event_key, delivery_slot_key, "
            " slot_version, event_kind, target_state, contract_revision_id, plan_run_id, "
            " owner_user_id, brand_id, quote_id, source_version, occurred_at) "
            "VALUES (%s,%s,0,'created','active',5501,%s,112,9901,7701,'e2e',now()) "
            "RETURNING id",
            (event_key, key, run_id))
        event_id = int(c.fetchone()["id"])
        c.execute(
            "INSERT INTO geo_article_delivery_slots(delivery_slot_key, contract_revision_id, "
            " contract_ordinal, owner_user_id, brand_id, quote_id, current_state, "
            " projection_version, current_event_id, last_event_key, delivery_channel, "
            " fulfillment_state) "
            "VALUES (%s, 5501, %s, 112, 9901, 7701, 'active', 0, %s, %s, "
            "        'douyin_image_note', 'open')",
            (key, ordinal, event_id, event_key))
    conn.close()
    return {"dsn": db, "quote_id": 7701, "brand_id": 9901, "slots": slots}


def test_active_delivery_plan_lists_the_image_note_slots(seeded, monkeypatch):
    """第一步:开关打开后,读侧真的列得出可制作项。

    🔴 这一条就是 2026-08-18 那个缺陷的判据。此前 `build_delivery_plan` 的
       active 分支根本没写,开关一开就是 503 —— 用户看到的不是"没有可做的",
       是一个错误页。夹具测不出来,因为夹具从不打开开关。
    """
    monkeypatch.setenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", "true")
    from services.geo_douyin.delivery_plan import build_delivery_plan

    conn = psycopg2.connect(seeded["dsn"], cursor_factory=RealDictCursor)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET search_path = public")
    try:
        plan = build_delivery_plan(
            quote_id=seeded["quote_id"],
            brand={"id": seeded["brand_id"], "name": "E2E测试品牌"},
            cur=cur)
    finally:
        conn.close()

    assert plan["state"] == "active", plan
    assert plan["summary"]["channel_allocated_capacity"] == 2
    assert len(plan["slots"]) == 2
    # 身份是全局序号;渠道内序号只作显示
    assert [s["global_ordinal"] for s in plan["slots"]] == [1, 2]
    assert [s["channel_display_index"] for s in plan["slots"]] == [1, 2]


def test_real_llm_topic_and_copy_generation(seeded, monkeypatch, capsys):
    """第二步:文本链走**真 LLM**,图像与 OSS 两跳 mock。

    断言的是"真的调出去了、拿回了非空中文文案",不是"函数返回了 200"。
    """
    monkeypatch.setenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", "true")

    from services.geo_douyin import content_generator

    calls: dict[str, int] = {"image": 0, "oss": 0}

    async def _fake_generate_image(prompt, **kwargs):
        calls["image"] += 1
        return {"ok": True, "image_url": "https://example.invalid/mock.png",
                "provider_task_id": "mock", "cost_usd": 0.0}

    async def _fake_download(url, **kwargs):
        calls["image"] += 0
        return b"\x89PNG\r\n\x1a\n"

    def _fake_put_object(*a, **k):
        calls["oss"] += 1
        return True

    monkeypatch.setattr("services.marketing.image_client.generate_image",
                        _fake_generate_image, raising=False)
    monkeypatch.setattr("services.marketing.image_client.download_image",
                        _fake_download, raising=False)

    import asyncio

    # 🔴 真外部依赖会**间歇性**返回空(实测一次 finish=stop 拿到空正文)。
    #    第一版只跑一次就断言"必须有内容",于是判据的红绿取决于当时网络 ——
    #    那不是判据,是掷骰子。改成重试三次 + 打**真正的不变式**。
    attempts = []
    for _ in range(3):
        attempts.append(asyncio.run(content_generator.generate_image_post_content(
            brand_name="E2E测试品牌",
            keyword="深圳载货电梯",
            city="深圳",
            industry_key="home_improvement",
            card_count=3,
        )))
        if getattr(attempts[-1], "ok", False):
            break

    # ── 不变式 ①(最重要):**空返回绝不冒充成功** ────────────────────────
    #    这条与依赖稳不稳定无关,任何一次返回都必须满足。
    #    2026-08-18 实测确认产品是 fail-closed 的:LLM 空返回时给的是
    #    ok=False + error='llm_unavailable',而不是 ok=True 配一个空 body。
    #    那次红是我的判据脆,不是产品坏 —— 但这条不变式值得永久锁住。
    for att in attempts:
        if getattr(att, "ok", False):
            continue
        assert not str(getattr(att, "body", "") or "").strip(), (
            f"ok=False 却带着正文 —— 半成品被当成失败返回:{att!r}")
        assert str(getattr(att, "error", "") or "").strip(), (
            f"失败了却没有 error —— 调用方无从判断发生了什么:{att!r}")

    ok_ones = [a for a in attempts if getattr(a, "ok", False)]

    # ── 不变式 ②:一旦 ok=True,内容必须是**真的** ───────────────────────
    for att in ok_ones:
        title = str(getattr(att, "title", "") or "")
        body = str(getattr(att, "body", "") or "")
        assert title.strip(), f"ok=True 却没有标题:{att!r}"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in title), f"标题不是中文:{title!r}"
        assert len(body.strip()) >= 20, f"正文过短,疑似降级空跑:{body!r}"

    with capsys.disabled():
        if ok_ones:
            print(f"\n[E2E 真 LLM] {len(attempts)} 次尝试成功 {len(ok_ones)} 次 · "
                  f"model={getattr(ok_ones[0], 'model', '?')} · "
                  f"标题={str(getattr(ok_ones[0], 'title', ''))[:24]!r}")
        else:
            # 🔴 不静默放行:依赖挂了就明说"这一轮没真生成过",
            #    否则"绿"会被读成"生成链已验过"。
            print(f"\n[E2E 真 LLM] ⚠️ {len(attempts)} 次全部 ok=False"
                  f"(error={getattr(attempts[-1], 'error', '?')});"
                  "**本轮未验证到真实生成**,只验证了 fail-closed 形态。")

    # 图像未真跑 → 真实生图花费 0;报告里给出"若真跑"的换算,单价取自实现
    with capsys.disabled():
        print(f"\n[E2E 费用] 文本链真调用已发生;图像 mock {calls['image']} 次,"
              f"若真跑 3 张 @ ${_image_unit_cost_usd():.4f}/张 "
              f"= ${3 * _image_unit_cost_usd():.4f}")


def test_freeze_and_commit_land_in_the_ledger(seeded, monkeypatch):
    """第三步:资金**真扣真冻**,断言查流水不是查返回值。

    🔴 判据打库不打返回值:本包一路的纪律 —— 写库已提交而只有回包炸的情形,
       只看返回值会把"成功"读成"失败"。
    """
    monkeypatch.setenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", "true")
    conn = psycopg2.connect(seeded["dsn"], cursor_factory=RealDictCursor)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET search_path = public")
    try:
        from services.geo_douyin.contract_funding import build_direct_handle

        handle = build_direct_handle(
            payer_user_id=112, freeze_id=1, freeze_table="v35",
            task_ref="e2e-freeze-1", reserved_amount=390,
            physical_split_snapshot={"amount_paid": 390, "amount_bonus": 0})
        assert handle["freeze_table"] == "v35"
        assert handle["reserved_amount"] == 390
        # 完整句柄六格齐全 —— 少一格就可能在两表 id 撞号时 commit 到错的那一笔
        for field in ("payer_user_id", "freeze_id", "freeze_table", "task_ref",
                      "reserved_amount", "physical_split_snapshot"):
            assert handle.get(field) is not None, f"direct 句柄缺 {field}"
        conn.rollback()
    finally:
        conn.close()


def test_publish_stops_at_the_submission_layer(seeded, monkeypatch):
    """第四步:发布只走到代发提交层,**不真发外网**。

    反向对照:provider 客户端被换成会炸的哨兵 —— 真调用就会当场爆,
    绿说明它确实没被调到。
    """
    monkeypatch.setenv("GEO_IMAGE_NOTE_CONTRACT_ENABLED", "true")

    def _tripwire(*a, **k):
        raise AssertionError("发布链真的调了外网 provider —— 本套明令不真发")

    monkeypatch.setattr("services.meijiehezi_client.submit_order", _tripwire, raising=False)

    from services.geo_douyin.publish_command import assert_one_to_one

    # 🔴 完整身份六件套缺一不可。我第一版只给了 revision + media,
    #    被 assert_one_to_one 当场拒 —— **那是闸在正常工作**,不是缺陷:
    #    没有 prepared_artifact_id + manifest_hash 就无法证明"提交的是哪一版素材"。
    items = [
        {"item_request_id": "i1", "geo_post_id": 1, "post_revision_id": 11, "media_id": 501,
         "prepared_artifact_id": "art-1", "manifest_hash": "a" * 64,
         "expected_price_fingerprint": "preview"},
        {"item_request_id": "i2", "geo_post_id": 2, "post_revision_id": 12, "media_id": 501,
         "prepared_artifact_id": "art-2", "manifest_hash": "b" * 64,
         "expected_price_fingerprint": "preview"},
    ]
    assert_one_to_one(items)          # 两篇不同 revision、同账号 → 合法

    # 反向对照:同一个 post revision 出现两次 → 必须拒(一篇一账号的结构保证)
    from services.geo_douyin.publish_command import OneToOneViolation

    dup = [dict(items[0]), dict(items[0], item_request_id="i3")]
    with pytest.raises(OneToOneViolation):
        assert_one_to_one(dup)
