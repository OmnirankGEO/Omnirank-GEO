"""Z-3.3 · 广告法**一键修复**(§0.5.6 Z-3.3 @ spec e710be6c2)。

Z-3.3 逐字::

    广告法 ``repair-finding``:默认 LLM **按 rule_id** 生成候选改写,
    销售确认/微调后成新 revision;**不得实现成纯手工文本框**。

一期就是退化成"跳通用编辑器"被判未兑现 —— 所以这个模块存在的全部理由,
是让第一颗按钮真的能给出**改好的句子**,而不是给她一个空输入框。

═══════════════════════════════════════════════════════════════════
🔴 「按 rule_id」不是修辞
═══════════════════════════════════════════════════════════════════
改写要求由**命中的那条规则**决定:命中的是哪几个绝对化用语,
prompt 里就逐个点名要求换掉;其余一字不改。

拿一句通用的"帮我改得合规一点"去改,结果有两个都不合格:
改出来的东西不针对这条规则(可能把别的地方也重写了),
而且**没法证明改到位** —— 我们无从判断这条候选还犯不犯规。

═══════════════════════════════════════════════════════════════════
🔴 候选必须**过一遍同一把尺子**才算候选
═══════════════════════════════════════════════════════════════════
LLM 改完之后再用 ``find_absolute_violations`` 扫一遍:还命中的直接丢。
不丢的话她会确认一条"改过了但仍然违规"的句子,然后在外调那一刻
被同一道门再拦一次 —— 她会以为系统坏了。

🔴 本文件**零词表、零正则、零阈值**:禁区只来自 Owner 签发包
   (与 ``publish/legal_gate`` 同一条纪律)。想扩门必须去改签发包。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger("GEO-DefGeoLegalRepair")

LEGAL_REPAIR_VERSION = "defgeo-legal-repair-v1"

#: [工单 E3-2] 正文 + 机审刷新那一跳的 SAVEPOINT 名。
#: 🔴 **固定常量**,不拼任何入参 —— 拼入参就是 SQL 注入面
#:    (与 ``run_ledger_bridge._SAVEPOINT`` 同规矩)。
_APPLY_SAVEPOINT = "sp_defgeo_legal_repair"

#: 一次给几条候选。**复数是刻意的**:只给一条等于替她做决定,
#: 而她比我们清楚这家客户能不能这么说。
CANDIDATE_COUNT = 3


class LegalRepairUnavailable(RuntimeError):
    """这次给不出候选。

    抛,不是返回空数组 —— 空数组会被前端画成"改好了,但一条都没有",
    她读不出是"没查到"还是"不用改"。
    """


def _hits(passage: str) -> tuple[str, ...]:
    """命中的绝对化用语。**词表只来自签发包**。

    🔴 取的是 ``LegalHit.term``(命中的那个词)而**不是** ``excerpt``。
       excerpt 是上下文切片(整句),塞进 prompt 的「必须换掉的表述」里
       会变成"把整句换掉" —— 那既不是按 rule_id 改,判据也没法断言
       "prompt 里逐个点名了命中词"。第一版写的就是 excerpt。
    """
    try:
        from services.marketing.legal_context import find_absolute_violations
    except Exception as exc:  # pragma: no cover - 环境缺件
        raise LegalRepairUnavailable(f"法律包不可用:{exc}") from exc
    return tuple(h.term for h in find_absolute_violations(passage or ""))


def candidate_prompt(*, rule_id: str, passage: str) -> str:
    """按 rule_id + 实际命中词构造改写要求。

    🔴 命中词进 prompt 是这条链"按 rule_id"的可验证形态:
       判据可以断言 prompt 里逐个出现了命中词。
       只写一句"请合规改写"的话,这条断言无处可打。
    """
    hits = _hits(passage)
    if not hits:
        # 没命中却来修 —— 说明调用方拿错了段落。如实拒,不硬编一句改写。
        raise LegalRepairUnavailable("这段话没有命中已签发的广告法目录")
    banned = "、".join(dict.fromkeys(hits))
    return (
        "下面这句话里有涉嫌违反广告法的绝对化表述,请改写。\n\n"
        f"规则编号:{rule_id}\n"
        f"必须换掉的表述:{banned}\n\n"
        "改写要求:\n"
        "1. 只改这一句,不要扩写、不要补充新事实、不要加数据;\n"
        "2. 把绝对化说法换成有依据的相对表述(例如换成可核实的范围、条件或来源);\n"
        "3. 保留原句要表达的意思与语气,长度接近;\n"
        f"4. 给出 {CANDIDATE_COUNT} 个不同写法,每行一个,不要编号、不要解释。\n\n"
        f"原句:{passage}"
    )


async def _default_llm(prompt: str) -> str:
    """现役 LLM 出口。与 ``server.py`` 的 span 修复走同一个 caller ——
    不给广告法修复单开一条模型路径(换路径必须同改提取器,代价很高)。"""
    from tools.multi_llm_caller import MultiLLMCaller

    text, _provider = await MultiLLMCaller(timeout=90.0, temperature=0.3).call(
        prompt, verbose=False)
    return text or ""


async def build_candidate_payload(
    *,
    rule_id: str,
    passage_excerpt: str,
    prompt: str,
    llm_fn: Callable[[str], Awaitable[str]] | None = None,
) -> list[dict[str, str]]:
    """出候选。每一条都**再过一遍同一把尺子**,还犯规的丢掉。

    返回 ``[{"text": ..., "note": ...}, ...]``;一条都不剩就抛。
    """
    call = llm_fn or _default_llm
    try:
        raw = await call(prompt)
    except Exception as exc:  # noqa: BLE001
        raise LegalRepairUnavailable(f"改写调用失败:{exc}") from exc

    lines = [ln.strip() for ln in str(raw or "").splitlines()]
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for line in lines:
        # 去掉模型偶尔加上的编号/项目符号 —— 只削前缀,不动句子本身。
        cleaned = line.lstrip("0123456789.、)】)-• \t").strip()
        if not cleaned or cleaned == passage_excerpt or cleaned in seen:
            continue
        if _hits(cleaned):
            # 🔴 改完还犯规 ⇒ 丢。她确认了也会在外调那一刻被同一道门拦下。
            logger.info("[defgeo-legal-repair] 候选仍命中禁区,丢弃:%s", cleaned[:40])
            continue
        seen.add(cleaned)
        out.append({
            "text": cleaned,
            # ══════════════════════════════════════════════════════════════
            # 🔴 [工单 C-5 · Owner 2026-08-25 选 A] **如实降级**的措辞
            # ══════════════════════════════════════════════════════════════
            # 上一版写的是「已避开这条规则点名的说法,**可以直接用**,
            # 也可以在这基础上改」。前半句是真的,后半句不是:
            # 这里唯一做过的核验是把候选**再过一遍 find_absolute_violations**
            # ——即 Owner 签发禁词表的那一把尺子。它不看标题、不看虚构门店数、
            # 不看未证实的数字,也不看这句话放回正文之后合不合逻辑。
            # 把"没检出禁词"讲成"可以直接用",是拿一个窄判定去背一个宽承诺;
            # 她照着发出去出了事,回头看这句话就是我们说可以的。
            #
            # 所以措辞只承诺**核过的那一格**,并把没核的那几类点名交还给她。
            # 🔴 别改成"仅供参考"这类免责话:那是往回退成"我们什么都不保证",
            #    而我们确实核过一格。说清边界,不是撇清责任。
            "note": ("已避开这条规则点名的词。"
                     "其它说法(数字、门店数、标题)我们这次没核,发之前你再看一眼。"),
        })
        if len(out) >= CANDIDATE_COUNT:
            break

    if not out:
        raise LegalRepairUnavailable("这次没有生成可用的改写")
    return out


# ══════════════════════════════════════════════════════════════════════════
# 🔴 [工单 C-5 · Codex 终审 P1-12] 「用这一句」必须**真的落成新 revision**
# ══════════════════════════════════════════════════════════════════════════
# 在这之前它没有落点:前端 ``LegalRepairPanel.onApply`` 只是
# ``navigate('/writing?revision=…&repaired=<改好的句子>')``,而**全仓没有任何
# 一个 `repaired` 查询参数的消费者**(grep 实核:只有这一个产出方)。
# 于是"用这一句"点完什么都没发生:重新确认冻的还是同一份正文、同一个
# ``articleHash``、同一个 ``canonicalHash``,发布门再拦一次。
# 她会以为系统坏了,而系统只是**从来没有把她的选择写下来**。
#
# 落法**不新造 revision 表**:本仓 defgeo 的 ``articleRevisionId`` 就是
# ``article:<articles.id>``(``publish_worker._article_id_of`` 是那条解析的唯一
# 实现),而现役 ``POST /api/articles/{id}/repair-finding`` 落盘的方式正是
# ``UPDATE articles SET content=…`` + ``refresh_article_review``。
# 所以"新 revision"在这个模型里就是**同一行的新正文**,它的
# ``body_hash`` 变了 —— 而 ``publish_worker.load_frozen_body`` 逐字节核对
# 冻结指纹,旧快照因此**自动失效**,重新预览/确认必然拿到新 hash。
# 这就是工单要的端到端判据:「点了之后 confirm 的内容 hash 变了」。


class LegalRepairApplyError(RuntimeError):
    """这一句没能落下去。**正文一个字不动**,并如实说原因。"""


class LegalRepairAmbiguousError(LegalRepairApplyError):
    """[工单 E3-2 · Codex 二审 P2] 这段原话在正文里出现了不止一处。

    🔴 旧行为是 ``text.find()`` 取**第一处**。偏移(``passage_ref``)在正文
       被改过之后就对不上,于是回落到 find —— 而"第一处"与"命中的那一处"
       只在原文只出现一次时才是同一处。重复段落(免责声明、栏目小标题、
       同一句卖点在两个小节各写一遍)在成稿里很常见,改错那一处的后果是:
       她看到"已修复",而真正违规的那一句原封不动地发出去了。

    所以多命中 ⇒ typed 歧义,**不猜**。继承 :class:`LegalRepairApplyError`
    是有意的:任何还没更新的调用方仍然会走"正文一个字不动"的那条路,
    而不是因为多了一个新异常类型漏进 500。
    """


class LegalRepairNotApplied(RuntimeError):
    """[工单 E3-2 · Codex 二审 P1-F7] 这一次修复**没有应用**,可以重试。

    与 :class:`LegalRepairApplyError` 的区别是**责任方**:
    ApplyError 说的是"你给的这一句/这个位置本身不行"(重试无用,
    她得改输入);本类说的是"我们这一跳没做成"(输入没问题,重试有用)。
    两者的对外 typed 结果因此不同:前者 422 不可重试,后者 503 可重试。

    抛出时保证:正文、article hash、review hash **三者全不变**
    (SAVEPOINT 回滚到 UPDATE 之前)。
    """


def locate_passage(content: str, *, passage_ref: str, passage_excerpt: str) -> tuple[int, int]:
    """在正文里定位要替换的那一段。定位不到就抛。

    两级定位,**先精确后回落**:

    1. ``passage_ref``(``rev:<id>#chars=<start>-<end>``,由
       ``publish.legal_gate._passage_ref`` 生成)给出的字符区间 ——
       但**必须**核对那一段确实等于 ``passage_excerpt``。不核对的话,
       正文在命中与修复之间被改过一个字,偏移就指到别处,
       我们会把一段**无关的话**替换掉。
    2. 偏移对不上时回落到 ``content.find(passage_excerpt)``(取第一处)——
       命中串在原文里是原样存在的,这比偏移可靠(现役
       ``writing_span_repair.locate_paragraph`` 也是这个理由用命中串)。

    两级都不成立 ⇒ 抛。**不猜**:猜错就是替换别人的句子。
    """
    text = str(content or "")
    needle = str(passage_excerpt or "")
    if not text or not needle:
        raise LegalRepairApplyError("正文或原句为空,定位不了要改的那一段")

    import re as _re

    m = _re.search(r"#chars=(\d+)-(\d+)$", str(passage_ref or ""))
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if 0 <= start < end <= len(text) and text[start:end] == needle:
            return start, end

    hit = text.find(needle)
    if hit < 0:
        raise LegalRepairApplyError(
            "这段原话已经不在当前正文里了 —— 中间有人改过稿,不按旧位置替换")
    # [工单 E3-2 · Codex 二审 P2] 多命中 ⇒ typed 歧义,**不取第一处**。
    #
    # 🔴 走到这里意味着上面那级精确偏移已经不成立(正文被改过);
    #    此时"第一处"与"当初命中违规的那一处"没有任何关系。
    #    改错一处的后果不是"少改了一句",是**她看到已修复而违规句原样发出去**。
    #    ``count`` 而不是再 find 一次:一次扫完,不引入第二个谓词。
    occurrences = text.count(needle)
    if occurrences > 1:
        raise LegalRepairAmbiguousError(
            f"这段原话在当前正文里出现了 {occurrences} 处,按旧位置又对不上 —— "
            "不猜改哪一处")
    return hit, hit + len(needle)


def apply_repair(
    cur, *, article_id: int, passage_ref: str, passage_excerpt: str, chosen_text: str,
) -> dict[str, Any]:
    """把她选中的那一句写进正文,返回新的 ``articleHash``。**调用方持有事务**。

    🔴 落库前**再过一遍同一把尺子**:她可以在候选基础上手改
       (``LegalRepairPanel`` 第二步就是那个输入框),改回一个禁词是可能的。
       候选生成时过了不代表最终定稿过了 —— 判定必须打在**真正要落库的那串**上。
    """
    from services.defensive_geo.publish import body_hash as _bh

    final = str(chosen_text or "").strip()
    if not final:
        raise LegalRepairApplyError("这一句是空的,没有可以落下去的内容")
    hits = _hits(final)
    if hits:
        raise LegalRepairApplyError(
            "这一句仍然命中已签发的广告法目录(%s),不落库" % "、".join(dict.fromkeys(hits)))

    cur.execute("SELECT content FROM articles WHERE id = %s FOR UPDATE", (int(article_id),))
    row = cur.fetchone()
    if row is None:
        raise LegalRepairApplyError(f"文章 {article_id} 不存在")
    original = str((row["content"] if not isinstance(row, tuple) else row[0]) or "")

    start, end = locate_passage(
        original, passage_ref=passage_ref, passage_excerpt=passage_excerpt)
    new_content = original[:start] + final + original[end:]
    if new_content == original:
        # 她选的就是原句 —— 落库等于什么都没做,而返回"已修复"会骗人。
        raise LegalRepairApplyError("这一句与原句一模一样,没有可落的改动")

    # ══════════════════════════════════════════════════════════════════
    # [工单 E3-2 · Codex 二审 P1-F7] 正文与机审刷新**同事务全成或全退**
    # ══════════════════════════════════════════════════════════════════
    # 这里原来是:先 UPDATE 正文,再用一个裸 ``except Exception`` 吞掉
    # ``refresh_article_review`` 的异常,然后**照样返回新 hash**。
    # 那段注释写着"刷新失败不回滚正文:正文已经改对了" —— 这句话在两条
    # 分支上都不成立:
    #
    #  ① 刷新抛的是 **DB 异常**(它的 SELECT/UPDATE 报错)⇒ psycopg2 把整个
    #     事务标成 aborted,调用方 ``get_db()`` 出块时那次 ``commit()``
    #     实际等于 ROLLBACK ⇒ **正文根本没改**,而 API 已经返回 200 +
    #     "已改进稿子里了" + 新 articleHash。她回去重新确认,什么都没变。
    #     (本仓自己的 ``scripts/sanitize_legacy_article_bodies.py`` 就把这一格
    #      逐字写在注释里:"后续 commit 实际等于 ROLLBACK —— 会把前面已改的
    #      稿子一起撤掉,而报表还在报'已清洗 N 篇' = 假成功"。)
    #  ② 刷新抛的是**普通异常**(不让事务失效)⇒ 正文提交、review hash 停在
    #     旧值 ⇒ 她重新确认时命中 ``content_changed_after_review``,
    #     而那条按 2026-08-01 §3A 是 ``H0_OPERATOR_HARD`` **不可覆盖** ——
    #     "改好了反而更发不出去",且现象查不到原因。
    #
    # 修法用本仓既有形态(``sanitize_legacy_article_bodies`` 逐字同款):
    # SAVEPOINT → 失败 ROLLBACK TO SAVEPOINT → typed「未应用+可重试」。
    # 🔴 SAVEPOINT 必须在 UPDATE **之前**:回滚点在 UPDATE 之后就撤不掉正文。
    # 🔴 ROLLBACK TO SAVEPOINT 也是把 aborted 事务救回来的**唯一**语句 ——
    #    调用方后面还要用这条连接(它是 ``get_db()`` 的),不救回来它连
    #    commit 都发不出去。
    cur.execute(f"SAVEPOINT {_APPLY_SAVEPOINT}")
    try:
        cur.execute("UPDATE articles SET content = %s WHERE id = %s",
                    (new_content, int(article_id)))

        # 修完必须刷新机审结论:只改 content 的话,``article_review_status``
        # 仍是修复前的 blocked ⇒「改好了发布门还拦」。现役 ``repair-finding``
        # 端点也是这么收尾的,这里复用**同一个**函数,不另写一份刷新逻辑。
        from services.article_review_gate import refresh_article_review

        refresh_article_review(int(article_id), cursor=cur)
    except Exception as exc:                              # noqa: BLE001
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {_APPLY_SAVEPOINT}")
            cur.execute(f"RELEASE SAVEPOINT {_APPLY_SAVEPOINT}")
        except Exception:                                 # pragma: no cover
            # 连回滚都发不出去 = 这条连接已经不可用。让它原样抛,
            # 调用方的 ``get_db()`` 会 rollback 整个事务 —— 结果同样是
            # "正文一个字没动",与本函数的承诺一致。
            raise
        logger.error(
            "[defgeo-legal-repair] 正文+机审刷新未能同事务完成,已整体回退"
            "(article=%s):%r", article_id, exc)
        raise LegalRepairNotApplied(
            "这一次没有改成,稿子一个字都没动") from exc
    cur.execute(f"RELEASE SAVEPOINT {_APPLY_SAVEPOINT}")

    return {
        "articleId": int(article_id),
        "articleHash": _bh.body_hash(new_content),
        "previousArticleHash": _bh.body_hash(original),
        "passageBefore": original[start:end],
        "passageAfter": final,
    }


def census() -> dict[str, Any]:
    return {
        "version": LEGAL_REPAIR_VERSION,
        "candidateCount": CANDIDATE_COUNT,
        # 可机读断言:本模块自己不带词表(禁区只来自签发包)。
        "ownsTermList": False,
        # [工单 C-5] 可机读断言:「用这一句」有真落点,且落点会改内容指纹。
        "applyWritesRevision": True,
        "applyReturnsNewArticleHash": True,
    }
