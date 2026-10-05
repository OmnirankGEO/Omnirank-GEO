"""
扩词规则降级路径判别测试(2026-07-26 · Deploy-CTO 切后实测发现)

背景:`_rule_filter` 是 `_llm_filter` 失败时的降级方案(keyword_expander.py:1879 / 1898)。
它里面有一句陈年写法:

    allowed = set(self._parse_cities(city))
    for c in allowed:
        allowed.add(province)      # ← 边迭代边改集合

这句一直休眠 —— 旧版 `_parse_cities('广东省深圳市龙岗区')` 返回整串地址,命中不了
`CITY_TO_PROVINCE`,循环体从不执行。本批 P0-3「整串地址必须先解析出市/省/区」把解析修好后,
它返回 `['深圳']` → 命中 `'广东'` → `RuntimeError: Set changed size during iteration`。

后果不是"筛得不准",而是**降级方案自己崩**:LLM 超时/配额/返空时,扩词从"退到规则筛选"
变成"整个 500"。安全网破洞比没有安全网更危险,因为它只在真出事时才暴露。

因此这里守两条:
  1. 整串地址(会被解析出省份的那种)进 `_rule_filter` 不得抛异常;
  2. 省份确实被加进了 allowed(证明修复没有把功能一起改没)。
"""

import pytest

from tools.keyword_expander import KeywordExpander


def _bare_expander() -> KeywordExpander:
    """不跑 __init__(它要外部 key/网络),只测纯函数行为。"""
    return KeywordExpander.__new__(KeywordExpander)


@pytest.mark.parametrize(
    "city",
    [
        "广东省深圳市龙岗区",  # 触发本次崩溃的真实值(brand 712 驰鲸)
        "深圳",
        "深圳市",
        "广东省深圳市",
        "北京市朝阳区",
    ],
)
def test_rule_filter_survives_parsable_address(city):
    """降级路径不得因为地址能被解析出省份就崩掉。"""
    expander = _bare_expander()
    keywords = [{"keyword": "深圳TikTok代运营哪家好"}, {"keyword": "上海SEO公司"}]
    try:
        result = expander._rule_filter(keywords, city, "科技推广和应用服务业")
    except RuntimeError as exc:  # noqa: PERF203 - 精确断言这一类失败
        pytest.fail(
            f"_rule_filter 在 city={city!r} 时崩溃({exc})。"
            "LLM 降级路径必须永远可用,否则 LLM 一挂扩词就整个 500。"
        )
    assert isinstance(result, list)


def test_province_still_added_to_allowed():
    """修复只改遍历方式,不得把「补省份」这个功能一起改没。

    判据:客户城市是深圳时,带「广东」的词不应被当外地词排除。
    """
    expander = _bare_expander()
    kept = expander._rule_filter(
        [{"keyword": "广东TikTok代运营"}], "广东省深圳市龙岗区", "科技"
    )
    assert [k["keyword"] for k in kept] == ["广东TikTok代运营"], (
        "省份没有被加进 allowed,本省词被误排除 —— 修复把功能改坏了"
    )


def test_foreign_city_still_excluded():
    """反证:外地大城市词仍要被排除,证明筛选没有整体失效。"""
    expander = _bare_expander()
    kept = expander._rule_filter(
        [{"keyword": "上海SEO公司"}, {"keyword": "深圳SEO公司"}],
        "广东省深圳市龙岗区",
        "科技",
    )
    kws = [k["keyword"] for k in kept]
    assert "上海SEO公司" not in kws, "外地城市词应被排除"
    assert "深圳SEO公司" in kws, "客户所在城市词不得被排除"
