"""#196 c1/c1b/c1c 判据 —— 素材准备失败时回包带**人话且脱敏**的原因。

面板原来只有前端自己那句「准备失败」。但把落库原话直接带出去也不行:
worker 写的是给机器看的,原样给服务商同时撞「说人话」与「供应商零暴露」两条红线。

c1c 修的是**类**不是实例。c1b 的判人话用的是「开头长得像 `SomeError: `
就换掉」的黑名单,对没见过的形状结构性失明 —— 而上线后最可能出现的那一句
(`publish_adapter.py:258` 的「中文前缀 + 异常尾巴」)恰好就是它没见过的形状。
本文件按**产出方枚举**取分母:`contract_worker.py:366-395` 一共只产出三种
reason,三种在这里各有判据;适配器那几句原话也各有判据。
"""
import ast
import io
import pathlib

import pytest

API_SRC = pathlib.Path("api/geo_image_note_api.py")
ADAPTER_SRC = pathlib.Path("services/geo_douyin/publish_adapter.py")

#: 上线后最可能出现的那一句。三个陷阱叠在一起:
#:  ① 中文前缀 —— 躲开「开头像异常」的黑名单;
#:  ② 主机名不在 `_VENDOR_TOKENS` 里 —— 躲开按名字认的 `scrub_text`;
#:  ③ 整条技术尾巴对服务商毫无意义。
ADAPTER_PROBE = ("第 2 张图上传失败:HTTPSConnectionPool("
                 "host='cdn.unknown-vendor.example', port=443)")

#: worker 在 `str(exc)` 为空时的产物(`contract_worker.py:394`)。
#: c1b 的正则要求冒号后面有空白,`.strip()` 之后这一条就不匹配了 —— 整条漏过去。
EMPTY_DETAIL_PROBE = "TimeoutError:"

GENERIC_FAILED = "素材准备失败,收起再展开面板会重新准备"
GENERIC_UNKNOWN = ("上传时连接中断,渠道可能已收到部分素材,"
                   "系统不会自动重传,请联系人工核对")


def _reason(artifact):
    from api.geo_image_note_api import _artifact_failure_reason
    return _artifact_failure_reason(artifact)


def _failed(*reasons, state="failed"):
    return {"state": state,
            "card_statuses": [{"index": i, "state": state, "reason": r}
                              for i, r in enumerate(reasons)]}


# ── 探针:上线后真会出现的两句,两个 state 各一 ────────────────────────
@pytest.mark.parametrize("state, expected", [
    ("failed", GENERIC_FAILED),
    ("unknown", GENERIC_UNKNOWN),
], ids=["state=failed", "state=unknown"])
def test_adapter_real_failure_string_never_reaches_the_agent(state, expected):
    """`publish_adapter.py:258` 那一句的真实形态,原样喂进来。

    它过了 `_neutral`(= `scrub_text`),所以「已经脱敏了」是**假的安全感**:
    词表里没有 `cdn.unknown-vendor.example`,整条技术尾巴原样留着。
    """
    out = _reason(_failed(ADAPTER_PROBE, state=state))
    assert out == expected
    for leak in ("HTTPSConnectionPool", "cdn.unknown-vendor.example", "host=", "443"):
        assert leak not in out, "技术细节 %r 漏到了对服务商的回包里" % leak


@pytest.mark.parametrize("raw", [EMPTY_DETAIL_PROBE, "TimeoutError: ", "OSError: "],
                         ids=["no-space", "trailing-space", "oserror"])
def test_exception_with_empty_detail_never_reaches_the_agent(raw):
    """`str(exc)` 为空时 worker 产出的就是「类名 + 冒号」、没有详情。

    这条单独立判据,是因为它**只差一个空白字符**就绕过了 c1b 的正则 ——
    而「少一个字符」正是上游行为变化时最容易发生的事。
    """
    out = _reason(_failed(raw, state="unknown"))
    assert out == GENERIC_UNKNOWN
    assert "Error" not in out and ":" not in out


# ── 适配器那几句纯中文原话:必须**原样通过** ───────────────────────────
@pytest.mark.parametrize("sentence, anchor", [
    ("没有可发布的图片", "没有可发布的图片"),
    ("第 1 张图读取为空", "张图读取为空"),
    ("图片没传上去，请稍后再试", "图片没传上去"),
], ids=["no-cards-at-all", "read-empty", "upload-host-not-allowed"])
def test_adapter_plain_chinese_passes_through_verbatim(sentence, anchor):
    """脱敏不能把有用的话一起吃掉。

    这三句是适配器给用户的**唯一**可执行信息(哪一张、卡在哪一步)。
    把它们换成通用句,用户就只知道「失败了」,不知道该重传还是该换图。

    🔴 `anchor` 在适配器源码里复核过 —— 判据的锚必须有对应物,
       否则改了措辞之后这里还在保护一句**没人产出**的话。
    """
    src = io.open(ADAPTER_SRC, encoding="utf-8").read()
    assert anchor in src, (
        "适配器已经不产出 %r 了 —— 锚过期,先去核对 publish_adapter.py" % anchor)
    assert _reason(_failed(sentence)) == sentence


# ── 通用句按 state 分:两句不可互换 ───────────────────────────────────
def test_generic_sentence_is_state_specific():
    """写反的后果不是「话术难看」,是让人去重传一个**渠道侧已有残留**的作品。

    `contract_worker.py:380-386`:fail-closed 的适配器失败时不回收已上传的图,
    所以 unknown 意味着「那边可能已经有半份」,重传会造重复素材。
    """
    got_failed = _reason(_failed(ADAPTER_PROBE, state="failed"))
    got_unknown = _reason(_failed(ADAPTER_PROBE, state="unknown"))
    assert got_failed != got_unknown, "两个 state 给了同一句 —— 分流没生效"

    assert "重新准备" in got_failed
    assert "人工" not in got_failed and "不会自动重传" not in got_failed, (
        "failed 是干净失败,不该叫人转人工")

    assert "不会自动重传" in got_unknown and "人工" in got_unknown
    assert "重新准备" not in got_unknown, (
        "unknown 侧可能已有残留,不能请用户再准备一次")


# ── 词表外的主机名:按形状脱敏(纵深)────────────────────────────────
def test_out_of_wordlist_host_is_scrubbed():
    """`scrub_text` 只认得词表里那几个名字。换一家渠道、对方换一个 CDN 域,
    词表当天就是过期的 —— 而**过期的词表不会报错**。

    这一句是纯中文 + 一个短主机名:它**不会**被白名单拦下
    (`x.cn` 没有 4 连续字母、没有符号),所以只有按形状脱敏那一道能救它。
    去掉 `scrub_hosts_and_urls` 这条就红。
    """
    out = _reason(_failed("渠道 x.cn 超时"))
    assert "x.cn" not in out, "词表外的主机名原样漏给了服务商"
    assert out == "渠道 外部发布通道 超时"


def test_host_glued_to_chinese_is_still_scrubbed():
    r"""[c1d · Review 毒 K] 中文**紧贴**主机名、中间没有空格。

    这是上游最常见的拼法,也是两版边界唯一分歧的地方:
    Review 规格写的 `\b` 在这里**不成立** —— 中文也是 `\w`,两边都是词字符
    就没有边界,`x.cn` 原样漏出去。现版用「非 ASCII 主机字符」的零宽断言。

    🔴 c1c 时我在报告里主张这版更强,却**没有判据钉它** —— 自述不是仪器。
       Review 把边界退回 `\b`,26/26 照样全绿。这条就是那颗钉子。
    """
    assert _reason(_failed("渠道x.cn超时")) == "渠道外部发布通道超时"


def test_letter_threshold_is_exactly_four():
    """[c1d · Review 毒 M] 恰好 4 个字母必须被拦下。

    阈值往上松一格就放行 `host` / `port` / `null` / `json` 这一类 ——
    全是 4 个字母,全是技术细节。Review 把阈值改成 5,26/26 照样全绿。

    ⚠️ 只钉**松的那一侧**。不钉「3 个字母必须放行」:往下收紧(4→3)是个
       合理的改法,钉住它等于让判据跟正确的修法互斥。
    """
    assert len("host") == 4, "夹具自证:这条钉的就是「恰好 4 个」这一格"
    assert _reason(_failed("host 超时", state="unknown")) == GENERIC_UNKNOWN


def test_full_url_is_scrubbed_whole():
    """整条 URL 要整条吃掉,不能只换 host 留下协议和路径。"""
    from services.publish_channel_privacy import scrub_hosts_and_urls
    assert scrub_hosts_and_urls("回调 https://a.example/cb?k=1 失败") == (
        "回调 外部发布通道 失败")


# ── 白名单不能退化成「只查 ://」────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "ConnectionResetError 连接被重置",   # 只有字母,没有 :// 也没有符号
    "余额不足 (0)",                      # 只有符号,没有 :// 也没有 4 连字母
], ids=["letters-only", "symbols-only"])
def test_whitelist_is_not_scheme_only(raw):
    """白名单的两半各自承重,少一半都会放行一类技术串。

    把判定退化成「只查 ://」时,这两条各自原样透出 —— 而 `://` 恰恰是
    三种产出里**最少见**的那个形状(裸异常串里根本没有)。
    """
    out = _reason(_failed(raw, state="unknown"))
    assert out == GENERIC_UNKNOWN, "技术形状 %r 没被拦下" % raw


# ── card_statuses 的字符串形态(psycopg2 有时回字符串)─────────────────
def test_card_statuses_accepts_json_string():
    """jsonb 列在某些 cursor 下回的是字符串而不是 list。

    这条分支在函数里一直存在,c1b 把钉它的判据删了 ——
    分支还在、没人验,坏了只会在生产上现形。
    """
    art = {"state": "failed",
           "card_statuses": '[{"index": 0, "state": "failed", "reason": "NO_CARDS"}]'}
    assert _reason(art) == "作品还没有出图,先把卡片做出来再准备发布"


def test_malformed_card_statuses_string_is_empty_not_a_crash():
    """坏 JSON 给空串 —— 面板少一行,而不是整个接口 500。"""
    assert _reason({"state": "failed", "card_statuses": "{不是 json"}) == ""


# ── 最后那道 contains_vendor_trace:护的是**绕过 scrub 的两条路** ──────
def test_vendor_guard_covers_the_lookup_table(monkeypatch):
    """查表命中的句子是我们自己写的常量,**不过 scrub_text**。

    🔴 Review 毒 G 证明:对上游原话而言这道是冗余的(与 scrub 共用词表,
       洗完必然为假),删掉 10/10 全绿。它真正唯一护得住的是这条路 ——
       所以判据必须从这条路进来,否则那一行就是「看起来有检查」的装饰。
    """
    import api.geo_image_note_api as m
    monkeypatch.setitem(m._ARTIFACT_REASON_HUMAN, "NO_CARDS", "请联系快易播客服")
    assert _reason(_failed("NO_CARDS")) == "", "表里被写进供应商名,兜底没拦住"


def test_vendor_guard_covers_the_generic_sentence(monkeypatch):
    """按 state 替换的两句同样绕过 scrub,同样只有这道兜底守得住。"""
    import api.geo_image_note_api as m
    monkeypatch.setitem(m._GENERIC_BY_STATE, "unknown", "媒介盒子那边没回执")
    assert _reason(_failed(ADAPTER_PROBE, state="unknown")) == ""


# ── 词表内的老路径:仍然只脱敏、不整句吃掉 ─────────────────────────────
def test_plain_words_are_scrubbed_not_dropped():
    assert _reason(_failed("媒介盒子未回执", state="unknown")) == "外部发布通道未回执"


def test_long_reason_is_truncated():
    out = _reason(_failed("超长" * 200))
    assert len(out) <= 121 and out.endswith("…")


def test_no_vendor_token_survives_to_the_response():
    """逐个 token 扫一遍出口。

    ⚠️ 这条只证明「没泄露」,不证明「被脱敏了」 —— 空串也满足它
    (命中白名单换成通用句、或兜底返空,都算过)。真正钉脱敏的是上面
    `test_plain_words_are_scrubbed_not_dropped` 与
    `test_out_of_wordlist_host_is_scrubbed`。
    """
    from services.publish_channel_privacy import _VENDOR_TOKENS, contains_vendor_trace

    for token in _VENDOR_TOKENS:
        out = _reason(_failed("准备失败,渠道 %s 返回异常" % token))
        assert not contains_vendor_trace(out), (
            "供应商标记 %r 漏到了对服务商的回包里" % token)


def test_code_string_becomes_a_sentence():
    assert _reason(_failed("NO_CARDS")) == "作品还没有出图,先把卡片做出来再准备发布"


# ── 取**第一条**(两条不同的 reason)────────────────────────────────
def test_first_non_ready_reason_wins():
    """多张卡各有原因时,面板一行只放得下一句 —— 钉住取的是**第一条**。

    不钉的话「取最后一条」也能让其它判据全绿,而那会显示一个与首因无关的原因。
    """
    assert _reason(_failed("第一条原因", "第二条原因")) == "第一条原因"


def test_ready_cards_are_skipped():
    art = {"state": "failed", "card_statuses": [
        {"index": 0, "state": "ready", "url": "u"},
        {"index": 1, "state": "failed", "reason": "第二张没出来"}]}
    assert _reason(art) == "第二张没出来"


# ── 反向对照 ─────────────────────────────────────────────────────────
def test_non_failed_state_has_no_reason():
    """少了它,「永远取第一条 reason」也能让主臂绿 ——
    而那会让一篇准备成功的作品挂着上一次的失败原话。"""
    assert _reason({"state": "ready", "card_statuses": [
        {"index": 0, "state": "failed", "reason": "不该出现"}]}) == ""
    assert _reason({"state": "preparing", "card_statuses": []}) == ""


def test_missing_reason_is_empty_not_a_guess():
    """没有原话给空串 —— **不编**。空的「失败原因:」比不显示更像坏了。"""
    assert _reason(_failed()) == ""
    assert _reason({"state": "failed",
                    "card_statuses": [{"index": 0, "state": "failed"}]}) == ""


# ── 接线臂:必须钉在**那个 handler 体内** ─────────────────────────────
def test_v2_handler_itself_returns_the_field():
    """🔴 钉到 `api_prepare_publish_media_v2` 的函数体内。

    上一版只断言「源码里某个字典有 failure_reason 且由该函数供」——
    Review 的毒 RP-B 把调用搬进**另一个**同样有该键的字典、把 v2 改成空串,
    判据照样全绿。「某处有」不等于「这一处有」。
    """
    tree = ast.parse(io.open(API_SRC, encoding="utf-8").read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "api_prepare_publish_media_v2"), None)
    assert fn is not None, "找不到 v2 handler —— 锚过期了"

    supplied = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "failure_reason":
                    supplied.append(v)
    assert supplied, "v2 handler 的回包里没有 failure_reason —— 函数写好了没接线"
    assert any(isinstance(v, ast.Call)
               and getattr(v.func, "id", "") == "_artifact_failure_reason"
               for v in supplied), (
        "v2 里的 failure_reason 不是由 _artifact_failure_reason 供的")
