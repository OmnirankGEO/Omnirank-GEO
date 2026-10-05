"""Hybrid 检索升级测试（离线,不依赖 DashScope,用手工注入的假 embedding）。

覆盖 Codex 4 条硬要求 + 加权:
- embedding 检索必须在身份池内算(普通用户拿不到 agent chunk)
- query_vec 失败 fallback 纯 BM25
- current_page 同页加权 / chunk 类型加权
- aux_text(截图抽取短文本)提升按钮/字段/报错召回
- 低置信兜底(route 卡有但内容不足 → 不硬答)
"""
import pytest

from db.kb_db import insert_chunk, clear_system_chunks, init_kb_tables
from api.xiaobang_api import (
    bm25_search, invalidate_kb_cache, _is_route_only_insufficient,
    _should_short_circuit_canned, _clip_attachment_text,
    _is_normal_user_agent_business_question, render_normal_user_agent_business_reject,
    XiaobangChatRequest, AUX_TEXT_MAX,
    ROUTE_MATCH_MULT, ROUTE_ONLY_MIN_CONTENT,
    SYSTEM_PROMPT_TPL,
)


# ======================================================================
# 分词器保真闸(2026-08-20 WO-D ① · 「两条 flaky」的真因)
# ======================================================================
#
# 🔴 这两条**根本不是 flaky**,是**装了 jieba 的机器全绿、没装的全红**:
#
#   `api.xiaobang_api.tokenize` 在 jieba 缺失时会**静默降级**成空格切分
#   (该函数 `except ImportError` 分支)。降级后中文整句变成一个 token,
#   凡是「用户问句本身不含命中词、召回全靠 aux_text/route」的判据一律返 [],
#   断在 `assert r` / `assert '/pz#btn' in _slugs(r)` 上 —— 看着像抖动,
#   实际与被测代码零因果。实测(2026-08-20 @ a88533306):
#     无 jieba → 12 passed / 2 failed,连跑 10 次**次次同样两条**;
#     有 jieba → 14 passed,连跑 20 次零抖动。
#
# 🔴 jieba 是**生产依赖**(requirements-docker.txt `jieba==0.42.1`),
#   所以「没装」是**环境缺陷**,不是一种合法配置 —— 本包已把它补进
#   requirements.txt(此前只在 docker 那份里,本地/CI 装不到)。
#
# 🔴 这道闸有两半,缺一不可:
#   ① `test_tokenizer_is_production_grade` —— **降级时必须有一条红**,
#      且红在「分词器」这个真因上,而不是散成两条看不懂的 `assert []`;
#   ② 两条依赖真分词的判据 `skipif` 显式隔离并注明 —— 它们不再以
#      「已知红」的身份混进 A/B 结果里冒充新增红。
#   只做②不做① = 把问题藏起来;只做①不做② = 红仍然散着。
try:
    import jieba as _jieba
except ImportError:
    _jieba = None

HAS_JIEBA = _jieba is not None
_NO_JIEBA_REASON = (
    "jieba 未安装 → api.xiaobang_api.tokenize 静默降级为空格切分,"
    "本条判据打的是「真分词下的召回」,降级态下它验不到任何东西。"
    "真因由 test_tokenizer_is_production_grade 单独报红;"
    "修法:pip install jieba==0.42.1(已在 requirements.txt)。"
)


def test_tokenizer_is_production_grade():
    """🔴 分词器必须是生产那一个(jieba),不是 ImportError 降级的空格切分。

    判据成对:
      · 必须命中 —— 真分词把「这个AI填写有啥用」切出 `填写`,
        把「提现噪音abc…」切出 `提现`(这正是那两条召回判据的前提);
      · 必须不命中 —— 降级态下同一串只会得到整句一个 token,
        `填写`/`提现` 都取不到。所以把 jieba 卸掉,这条**必然红**。
    """
    assert HAS_JIEBA, (
        "jieba 未安装。本目录所有 BM25 召回判据都在**降级分词器**上跑,"
        "绿的那些也不代表生产行为。jieba 是生产依赖"
        "(requirements-docker.txt / requirements.txt 均已声明 jieba==0.42.1),"
        "缺失是环境缺陷 —— 装上再判色。"
    )
    from api.xiaobang_api import tokenize

    assert "填写" in tokenize("这个AI填写有啥用 要钱吗")
    assert "提现" in tokenize("这个怎么用 " + "提现" + "噪音abc" * 10)
    # 反向:降级态的特征是「整句成一个 token」。真分词绝不会这样。
    assert len(tokenize("这个AI填写有啥用")) >= 2


def _seed(rows):
    init_kb_tables()
    clear_system_chunks()
    for r in rows:
        insert_chunk(
            source_type=r["source_type"], source_slug=r["slug"], source_title=r.get("title", "页"),
            content=r["content"], section_title=r.get("section"), route=r.get("route"),
            category="system", is_admin_only=False, token_keywords=r["tokens"],
            embedding=r.get("embedding"), origin="manual", visible_to=r["visible_to"],
        )
    invalidate_kb_cache()


def _slugs(results):
    return [c["source_slug"] for c, _ in results]


def test_embedding_pool_isolation_normal_user():  # 🔴 安全红线
    """普通用户 embedding 检索绝不能命中 agent chunk —— 即便其向量与 query 完全一致。"""
    _seed([
        {"source_type": "sys_field", "slug": "/p#普通", "content": "报价 普通字段", "tokens": ["报价"],
         "embedding": [1.0, 0.0, 0.0], "visible_to": "both", "route": "/p"},
        {"source_type": "sys_field", "slug": "/p#代理", "content": "报价 代理私有", "tokens": ["报价"],
         "embedding": [1.0, 0.0, 0.0], "visible_to": "agent", "route": "/p"},
    ])
    qv = [1.0, 0.0, 0.0]  # 与两条 chunk 向量都完全一致
    n = bm25_search("报价", is_admin=False, query_vec=qv, identity="normal_user")
    assert "/p#代理" not in _slugs(n)   # agent chunk 物理不在普通池
    assert "/p#普通" in _slugs(n)
    a = bm25_search("报价", is_admin=False, query_vec=qv, identity="agent")
    assert "/p#代理" in _slugs(a)       # 代理池能拿到
    clear_system_chunks()


def test_query_vec_fallback_to_bm25():
    """query_vec=None(embedding 失败)→ 纯 BM25 仍可用,不崩。"""
    _seed([
        {"source_type": "sys_qa", "slug": "/p#qa", "content": "报价怎么填 答：填客户名", "tokens": ["报价", "填"],
         "visible_to": "both", "route": "/p"},
    ])
    r = bm25_search("报价", is_admin=False, query_vec=None, identity="normal_user")
    assert r and "/p#qa" in _slugs(r)
    clear_system_chunks()


def test_route_match_boost():
    """current_page 同 route 的 chunk 被加权抬升排名。"""
    _seed([
        {"source_type": "sys_page", "slug": "/pa", "content": "报价 页面甲", "tokens": ["报价"], "route": "/pa", "visible_to": "both"},
        {"source_type": "sys_page", "slug": "/pb", "content": "报价 页面乙", "tokens": ["报价"], "route": "/pb", "visible_to": "both"},
    ])
    r = bm25_search("报价", is_admin=False, identity="normal_user", current_page="/pa")
    assert r[0][0]["route"] == "/pa"   # 同页加权 → 排第一
    clear_system_chunks()


def test_chunk_type_boost_qa_over_page():
    """同 route 同词,sys_qa(×1.3)排在 sys_page(×1.0)前。"""
    _seed([
        {"source_type": "sys_page", "slug": "/px#page", "content": "报价说明", "tokens": ["报价"], "route": "/px", "visible_to": "both"},
        {"source_type": "sys_qa", "slug": "/px#qa", "content": "报价说明", "tokens": ["报价"], "route": "/px", "visible_to": "both"},
    ])
    r = bm25_search("报价", is_admin=False, identity="normal_user")
    assert r[0][0]["source_type"] == "sys_qa"
    clear_system_chunks()


def test_aux_text_improves_button_recall():
    """截图抽取的辅助文字(按钮名)并入 query,提升 sys_button 召回。"""
    _seed([
        {"source_type": "sys_button", "slug": "/py#btn-提现", "content": "按钮「提现」：转出佣金", "tokens": ["提现"],
         "route": "/py", "visible_to": "agent"},
    ])
    # 用户问题不含"提现" → 召不到
    no_aux = bm25_search("这个怎么操作", is_admin=False, identity="agent")
    assert "/py#btn-提现" not in _slugs(no_aux)
    # 截图抽到按钮名"提现"作 aux_text → 召回
    with_aux = bm25_search("这个怎么操作", is_admin=False, identity="agent", aux_text="提现")
    assert "/py#btn-提现" in _slugs(with_aux)
    clear_system_chunks()


def test_screenshot_aux_recall_publish_subpage_button():
    """截图抽到子页/按钮文案时,即使用户只问"这个有啥用",也应召回对应子页按钮。"""
    _seed([
        {"source_type": "sys_button", "slug": "/publish#btn-发布参谋", "content": "按钮「发布参谋」：查看行业引用数据,选择行业后刷新引用记录和测试词搜索。",
         "tokens": ["发布参谋", "引用", "行业"], "route": "/publish", "visible_to": "both"},
        {"source_type": "sys_page", "slug": "/diagnosis/new", "content": "新建诊断页面", "tokens": ["诊断"], "route": "/diagnosis/new", "visible_to": "both"},
    ])

    r = bm25_search("这个有啥用", is_admin=False, identity="normal_user", current_page="/publish", aux_text="发布参谋 引用记录 行业列表")

    assert r[0][0]["source_slug"] == "/publish#btn-发布参谋"
    clear_system_chunks()


@pytest.mark.skipif(not HAS_JIEBA, reason=_NO_JIEBA_REASON)
def test_screenshot_aux_recall_error_without_internal_code():
    """带图问按钮/字段时,召回内容应是用户话术,不带内部功能码。"""
    _seed([
        {"source_type": "sys_qa", "slug": "/diagnosis/new#qa-ai-fill", "content": "AI 填写有什么用 答：自动补全行业、关键词、客户区域等字段,成功后才扣 40 算力。",  # [R3-P7 ①] 原为「40 积分」,运行时术语门会拒;标记语义不变
         "tokens": ["AI", "填写", "补全", "扣"], "route": "/diagnosis/new", "visible_to": "both"},
    ])

    r = bm25_search("这个AI填写有啥用 要钱吗", is_admin=False, identity="normal_user", current_page="/diagnosis/new", aux_text="AI填写 按钮")

    assert r
    content = r[0][0]["content"]
    assert "自动补全" in content
    assert "brand_fill" not in content
    clear_system_chunks()


def test_route_only_insufficient_handoff():
    """有 route 页面卡但检索内容分低于阈值 → 低置信兜底(不硬答)。"""
    fake_card = {"source_title": "我的钱包", "route": "/wallet"}
    # 无任何内容命中
    assert _is_route_only_insufficient(fake_card, [], 0.0) is True
    # 内容分低于阈值
    assert _is_route_only_insufficient(fake_card, [({"x": 1}, 0.3)], 0.3) is True
    # 内容分足够 → 正常答,不兜底
    assert _is_route_only_insufficient(fake_card, [({"x": 1}, 5.0)], 5.0) is False
    # 没有页面卡 → 不走这个兜底(走普通软拒)
    assert _is_route_only_insufficient(None, [], 0.0) is False


# ---------- attachment_text 接线(Codex 复核两点)----------

def test_request_accepts_attachment_text():
    """XiaobangChatRequest 能真正接收 attachment_text(不再被 pydantic 忽略)。"""
    req = XiaobangChatRequest(message="这个按钮为什么灰了", current_page="/wallet", attachment_text="提现 未实名")
    assert req.attachment_text == "提现 未实名"
    # 缺省为 None
    req2 = XiaobangChatRequest(message="hi")
    assert req2.attachment_text is None


def test_canned_not_short_circuited_with_attachment():
    """带 attachment_text(有上下文)→ 不短路 canned,交完整 RAG。"""
    # 无 current_page 无 attachment → 允许短路 canned
    assert _should_short_circuit_canned(
        XiaobangChatRequest(message="在吗")) is True
    # 有 attachment_text → 不短路(带上下文走 RAG)
    assert _should_short_circuit_canned(
        XiaobangChatRequest(message="在吗", attachment_text="提现按钮")) is False
    # 有 current_page → 同样不短路
    assert _should_short_circuit_canned(
        XiaobangChatRequest(message="在吗", current_page="/wallet")) is False


@pytest.mark.skipif(not HAS_JIEBA, reason=_NO_JIEBA_REASON)
def test_attachment_text_truncated_consistently():
    """超长 attachment_text → 进 BM25/embedding 的都是同一截断版本(≤AUX_TEXT_MAX)。"""
    long_ocr = "提现" + "噪音abc" * 500
    clipped = _clip_attachment_text(long_ocr)
    assert len(clipped) == AUX_TEXT_MAX          # 入口统一截断
    assert _clip_attachment_text(None) == ""
    assert _clip_attachment_text("  报错原文  ") == "报错原文"
    # bm25_search 内部对 aux_text 同样有截断兜底(双保险,不会因长文本炸召回)
    _seed([
        {"source_type": "sys_button", "slug": "/pz#btn", "content": "按钮「提现」：转出", "tokens": ["提现"],
         "route": "/pz", "visible_to": "agent"},
    ])
    r = bm25_search("这个怎么用", is_admin=False, identity="agent", aux_text=clipped)
    assert "/pz#btn" in _slugs(r)   # 截断后仍保留了开头的"提现",能召回
    clear_system_chunks()


def test_attachment_text_is_passed_to_final_prompt():
    """截图文字不能只参与召回,还必须进最终 prompt,否则"这个有啥用"会丢失子页定位。"""
    screenshot_context = (
        "【截图定位信息】\n"
        "截图显示发布中心顶部 Tab:发布参谋;页面标题 GEO引擎调研中心;行业列表有房地产、汽车。\n"
        "请把这段信息当作用户截图里的页面细节,用来判断他指的是哪个子页面、按钮、字段、状态或报错。"
    )
    prompt = SYSTEM_PROMPT_TPL.format(
        brand="OmniRank",
        assistant="小榜",
        context="《发布中心》\n发布参谋:按行业查看 AI 引用数据。",
        screenshot_context=screenshot_context,
        question="这个有啥用处嘛？",
    )

    assert "【截图定位信息】" in prompt
    assert "发布参谋" in prompt
    assert "GEO引擎调研中心" in prompt
    assert "优先用它判断用户正在问哪个子页面" in prompt
    assert "不要对用户说\"参考资料\"" in prompt


def test_normal_user_agent_business_question_rejected():
    """普通用户问代理经营/利润/成本/分账 → 命中前置拒答;普通用户自己的业务问题不命中。"""
    hit = [
        "代理怎么赚钱", "代理如何赚钱", "代理靠什么赚钱", "代理利润有多少",
        "服务方怎么赚钱", "出厂价是多少", "进货价怎么算", "报价系数在哪设",
        "加价倍率", "毛利怎么算", "代理提现", "代理结算", "渠道服务费", "白标怎么开",
    ]
    for q in hit:
        assert _is_normal_user_agent_business_question(q), f"应命中拒答: {q}"

    miss = [
        "怎么充值", "诊断要扣多少积分", "怎么改密码", "监测多久跑一次",
        "我的工具额度在哪看", "推荐有礼怎么用",
    ]
    for q in miss:
        assert not _is_normal_user_agent_business_question(q), f"不该命中: {q}"


def test_agent_business_reject_message_is_safe():
    """拒答文案本身不能反过来泄漏代理赚钱方式,且引导普通用户关注自己的额度。"""
    msg = render_normal_user_agent_business_reject()
    # [R3-P7 ①] 原断言打的是「工具额度」—— 裁定 ② 域死池名,拒答文案已改成「算力」。
    # 不变式没变(仍要引导普通用户关注自己的那份),改的是**词** ⇒ 改断言不退役。
    assert "经营规则" in msg and "算力" in msg
    assert "额度" not in msg, "拒答文案又用回了死池名"
    for leak in ("28%", "进货", "差价", "出厂价", "佣金", "提现"):
        assert leak not in msg, f"拒答文案泄漏了: {leak}"
