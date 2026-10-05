"""发布公开 URL 的核实链 —— **谁有资格宣布"发布过了"**。

WO_ARTICLE_BROWSER_SELF_REPORT_2026-08-19 · R2(Review-CTO 2026-08-19 §①)。

## R1 错在哪(R2 的起点)

R1 让服务端探针去取自报的 URL,页面上有我方标题就升 `verified`。
Review + Codex 复审一句话打穿:**攻击者做一个同标题的页面就够了**。
探针能证明的是"某个页面存在且写着这个标题",而任何人都能造出这样的页面 ——
它离"这篇文章确实发布在该平台该账号下"差着一整个信任层。

所以 R2 把探针**降级成线索**:

| 态 | 含义 | 能不能当终态事实 |
|---|---|---|
| `unverified` | 没有可核实的 URL 声明 | ❌ |
| `pending` | 有声明,排队等探针 | ❌ |
| `content_matched` | 探针在**平台域清单内**取到页面且带我方指纹 | ❌ **线索而已** |
| `needs_action` | 探不动 / 无指纹 / 域不在清单 / 无锚点 | ❌ 等人处理 |
| `verified` | 唯一终态事实 | ✅ |

而 `verified` **只有两个来源**:

  · `provider_receipt`   —— 供应商/渠道回执(代发 lane 走这条;自助发布这条链
                             天然没有回执,所以自助发布实际只剩下面一条路)
  · `human_attestation`  —— 人工核实动作,必须带 **actor + 证据 hash + 审计流水**

这也顺带解决了 R1 留下的"`needs_action` 无出路"——人工核实就是出路,但它带留痕。

🔴 这条规则**钉在库上**:`publish_records_verified_requires_authority_source`
   CHECK 让 `state='verified' AND source='server_probe'` 直接 23514。
   本模块的 Python 断言只是第一道;库那道才是"代码写错也拦得住"的那道。

## 探针的判据边界(别把它读成"能证明文章发出去了")

`content_matched` 只表示:URL 的域在该平台的合法域清单内、公网可取、页面上带
我方提交的标题。它**不表示**发布时间、账号归属、内容此后没被改,更不表示
那个页面是我们发的。要升 `verified` 必须有人看过并留下证据。

## R3 §① —— 两根轴,以及 verified 的单调性

R2 之后 Codex 复现出一个 P0:一条**人工核实**过的记录,管理员再点一次「重核」,
探针探不动就把它写回 `needs_action`、连 `public_url_verified_at` 都清成 NULL。
探针不是权威(上面刚定的),它凭什么撤销权威的结论?

根因是**把两个互不蕴含的命题压进了一根轴**:

| 轴 | 列 | 回答 | 单调性 |
|---|---|---|---|
| 核实轴 | `public_url_verification_state` | 当初到底发没发出去 | ✅ 只进不退,`verified` 是终态 |
| 可达轴 | `public_url_availability_state` | 那个页面**此刻**还在不在 | ❌ 本来就会来回变 |

页面 502 不蕴含当初没发;页面能打开也不蕴含当初发过。所以重核对已 `verified`
的行**照探不误**,只是结果落可达轴。「被平台下架 / 客户要求撤回」是人才能下的
结论,走 :func:`record_availability_attestation_with_cursor` 落 `retracted`。

守卫钉在 :func:`_write_state`(唯一写核实轴的收口),不钉在每个调用点 ——
调用点会新增,而"每个新调用点都记得自己判一下"这种约定必然漏一个。
库里还有 `trg_publish_records_verification_monotonic` 触发器兜底。

## R4 —— 两根轴各自补齐"库级后盾",不留只靠自觉的那一半

| 命题 | 收口处 | 库级后盾 |
|---|---|---|
| verified 只能来自权威两来源 | `_write_state` 断言 | `..._verified_requires_authority_source` |
| verified 不许降级 | `_write_state` 守卫 | `trg_..._verification_monotonic` |
| **verified_at 有值 ⇔ state=verified** | `_write_state` 的 CASE | `..._verified_at_requires_verified_state`(R4 §A) |
| **retracted 只能来自人工** | `_write_availability` 断言 | `..._retracted_requires_human_source`(R4 §B) |

§A 堵的是"首设直写":触发器只在 `OLD.verified_at IS NOT NULL` 时保值,
从 NULL 到有值那一跳原本没人管,而下游按 `verified_at IS NOT NULL` 判"核实过"
是很自然的写法 —— 那就成了绕过整条权威链的后门。
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Callable
from urllib.parse import urljoin

from psycopg2.extras import Json

from services.extension_platform_registry import url_is_within_platform_domain
from services.safe_https_probe import (
    _assert_safe_probe_hop,
    pinned_https_get_body,
)

logger = logging.getLogger("GEO-PublicationURLVerifier")

STATE_UNVERIFIED = "unverified"
STATE_PENDING = "pending"
STATE_CONTENT_MATCHED = "content_matched"
STATE_NEEDS_ACTION = "needs_action"
STATE_VERIFIED = "verified"

#: 闭集。与迁移里的 CHECK **同一份口径**,任一侧加值另一侧必须同步。
VERIFICATION_STATES = frozenset({
    STATE_UNVERIFIED, STATE_PENDING, STATE_CONTENT_MATCHED,
    STATE_NEEDS_ACTION, STATE_VERIFIED,
})

SOURCE_SERVER_PROBE = "server_probe"
SOURCE_PROVIDER_RECEIPT = "provider_receipt"
SOURCE_HUMAN_ATTESTATION = "human_attestation"
VERIFICATION_SOURCES = frozenset({
    SOURCE_SERVER_PROBE, SOURCE_PROVIDER_RECEIPT, SOURCE_HUMAN_ATTESTATION,
})

#: 🔴 R2 §① 的核心:能签发 `verified` 的来源**只有这两个**。探针不在其中。
AUTHORITY_SOURCES_FOR_VERIFIED = frozenset({
    SOURCE_PROVIDER_RECEIPT, SOURCE_HUMAN_ATTESTATION,
})

#: 唯一可当终态事实的态。下游高危消费方一律以它为闸。
TERMINAL_FACT_STATES = frozenset({STATE_VERIFIED})

#: 探针**只能**产出这三种结论 —— 它够不到 verified。
PROBE_REACHABLE_STATES = frozenset({
    STATE_UNVERIFIED, STATE_CONTENT_MATCHED, STATE_NEEDS_ACTION,
})

PROBE_MAX_REDIRECTS = 3
PROBE_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
#: 指纹锚点最短长度。太短的标题在任意页面上都能"命中",那种命中没有判别力。
MIN_FINGERPRINT_ANCHOR_LEN = 6

PROBE_METHOD = "server_probe_content_match_v2"

# ── 可达轴(R3 §①)────────────────────────────────────────────────────────
# 🔴 与核实轴是**两个互不蕴含的命题**:
#      核实轴 = 当初到底发没发出去(单调,只进不退)
#      可达轴 = 那个页面**此刻**还在不在
#    R2 之后仍有一个 P0:已 verified 的行被管理员再点一次「重核」,探针探不动就把
#    它写回 needs_action、连 verified_at 都清成 NULL —— 一次探测抹掉了历史事实。
#    探针不是权威(R2 §① 已定),它凭什么撤销权威的结论?所以失效信息落这根轴。
AVAILABILITY_AVAILABLE = "available"
AVAILABILITY_UNREACHABLE = "unreachable"
AVAILABILITY_CONTENT_MISSING = "content_missing"
AVAILABILITY_DOMAIN_MISMATCH = "domain_mismatch"
AVAILABILITY_RETRACTED = "retracted"
AVAILABILITY_STATES = frozenset({
    AVAILABILITY_AVAILABLE, AVAILABILITY_UNREACHABLE, AVAILABILITY_CONTENT_MISSING,
    AVAILABILITY_DOMAIN_MISMATCH, AVAILABILITY_RETRACTED,
})
#: 只有人能下"撤回/下架"这个结论 —— 探针看到 404 只能说"探不动"。
AVAILABILITY_HUMAN_ONLY_STATES = frozenset({AVAILABILITY_RETRACTED})

#: 探针结论 → 可达轴。key 是 `_probe_verdict` 给的 reason。
_PROBE_REASON_TO_AVAILABILITY = {
    "content_fingerprint_matched": AVAILABILITY_AVAILABLE,
    "probe_non_200": AVAILABILITY_UNREACHABLE,
    "probe_failed": AVAILABILITY_UNREACHABLE,
    "fingerprint_absent_on_page": AVAILABILITY_CONTENT_MISSING,
    "no_fingerprint_anchor": AVAILABILITY_CONTENT_MISSING,
    "final_url_outside_platform_domain": AVAILABILITY_DOMAIN_MISMATCH,
    "url_outside_platform_domain": AVAILABILITY_DOMAIN_MISMATCH,
}


def _norm(text: Any) -> str:
    """归一化到「去掉所有空白 + casefold」,跨 HTML 排版差异比对标题。"""
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row else {}


def _default_fetcher(url: str) -> dict[str, Any]:
    """逐跳安全跳转 + pinned IP 拉正文;返回 {status, body, final_url, hops}。

    每一跳都过 `_assert_safe_probe_hop`(https + 443 + 公网 IP),与白标外链探测
    共用同一条防线 —— 不另开一条出站路径。
    """
    current = url
    for hop in range(PROBE_MAX_REDIRECTS + 1):
        host, pinned_ips, target = _assert_safe_probe_hop(current)
        status, headers, body = pinned_https_get_body(host, pinned_ips, target)
        if status in PROBE_REDIRECT_STATUSES:
            location = str(headers.get("location") or "").strip()
            if not location:
                raise ValueError("重定向缺少 Location")
            # 🔴 R3 §⑥:`Location` 合法地可以是相对路径(`/a/123`)甚至协议相对
            #    (`//host/p`)。直接拿去当下一跳 URL,`_assert_safe_probe_hop` 会因为
            #    "没有 scheme/host" 直接判失败 —— 于是一次**正常的站内 302** 被记成
            #    「探不动」。照 api/referral_api.py:660 的现成写法用 urljoin 解析。
            #    urljoin 出来的绝对 URL 仍然逐跳过安全校验,防线一格没少。
            current = urljoin(current, location)
            continue
        return {"status": status, "body": body, "final_url": current, "hops": hop}
    raise ValueError("重定向层数超限")


def _probe_verdict(record: dict[str, Any], fetched: dict[str, Any]) -> dict[str, Any]:
    """探针能给出的最高结论 = `content_matched`。判据在这里单点收口。"""
    status = int(fetched.get("status") or 0)
    if status != 200:
        return {"state": STATE_NEEDS_ACTION, "reason": "probe_non_200", "http_status": status}

    # 落地页也要在平台域内 —— 否则「站内跳转到攻击者站点」能绕过入口那道域校验。
    final_url = str(fetched.get("final_url") or "").strip()
    platform = record.get("platform")
    if final_url and not url_is_within_platform_domain(platform, final_url):
        return {"state": STATE_NEEDS_ACTION, "reason": "final_url_outside_platform_domain",
                "http_status": status}

    anchor = _norm(record.get("submitted_title_snapshot"))
    if len(anchor) < MIN_FINGERPRINT_ANCHOR_LEN:
        # 🔴 没有够长的锚点 = **没有判据**,不是"判据通过"。任何"锚点缺失就放行"
        #    的写法都会让自报凭一个能打开的 URL 混上去。
        return {"state": STATE_NEEDS_ACTION, "reason": "no_fingerprint_anchor",
                "http_status": status}

    if anchor not in _norm(fetched.get("body")):
        return {"state": STATE_NEEDS_ACTION, "reason": "fingerprint_absent_on_page",
                "http_status": status}

    # 🔴 到这里也只是 content_matched。攻击者做个同标题页面就能走到这一步 ——
    #    所以它是**线索**,给人看的,不是给严格链吃的。
    return {"state": STATE_CONTENT_MATCHED, "reason": "content_fingerprint_matched",
            "http_status": status}


class TerminalStateDowngradeRefused(RuntimeError):
    """把已 `verified` 的行写回低态 —— 拒绝,并且**响亮地**拒绝。

    静默忽略会让调用方以为写成功了;raise 会打断探针清扫。所以这个异常只在
    "调用方明确说要写核实轴"时抛,而探针路径在进来的第一步就已经改道去写可达轴,
    根本走不到这里(那条路是正常业务,不是异常)。
    """


def _current_verification_state(cur, record_id: int) -> str | None:
    cur.execute(
        "SELECT public_url_verification_state FROM publish_records WHERE id = %s",
        (int(record_id),),
    )
    row = _row_dict(cur.fetchone())
    return row.get("public_url_verification_state") if row else None


def _write_state(cur, record_id: int, state: str, *, source: str | None,
                 method: str | None, detail: dict[str, Any]) -> None:
    assert state in VERIFICATION_STATES, f"未知核实态:{state}"
    assert source is None or source in VERIFICATION_SOURCES, f"未知权威来源:{source}"
    # 🔴 R2 §① 第一道闸(库那道 CHECK 是第二道,两道都要在):
    #    verified 只认 provider 回执 / 人工核实。
    assert state != STATE_VERIFIED or source in AUTHORITY_SOURCES_FOR_VERIFIED, (
        f"verified 不能由 {source} 签发 —— 只有 {sorted(AUTHORITY_SOURCES_FOR_VERIFIED)} 可以"
    )
    # 🔴🔴 R3 §① 的 P0 守卫,**钉在唯一写核实轴的收口处**:
    #      verified 是终态,任何低态写入一律拒绝。
    #      为什么必须在这里而不是在每个调用点:调用点会新增(下一版加个供应商回执
    #      回调就是第四个),而"每个新调用点都记得自己判一下"这种约定必然漏。
    #      本仓的原话:同一谓词写两处 ⇒ 必有一处没人验。
    current = _current_verification_state(cur, record_id)
    if current == STATE_VERIFIED and state != STATE_VERIFIED:
        logger.error(
            "拒绝把已核实记录降级 record=%s %s→%s(页面事后失效属于可达轴,"
            "不动历史发布事实)", record_id, current, state)
        raise TerminalStateDowngradeRefused(
            f"record={record_id} 已是 verified 终态,不接受降级为 {state}")
    cur.execute(
        """
        UPDATE publish_records
           SET public_url_verification_state  = %s,
               public_url_verification_source = %s,
               public_url_verification_method = %s,
               public_url_verification_detail = %s,
               -- 🔴 verified_at 只在**第一次**核实时落,之后 COALESCE 保住原值:
               --    它记的是"当初什么时候被核实的",不是"最近谁点过按钮"。
               --    原来那句 `ELSE NULL` 正是 P0 的另一半 —— 降级顺手把它抹了。
               public_url_verified_at = CASE WHEN %s = 'verified'
                                             THEN COALESCE(public_url_verified_at,
                                                           CURRENT_TIMESTAMP)
                                             ELSE public_url_verified_at END
         WHERE id = %s
        """,
        (state, source, method, Json(detail), state, int(record_id)),
    )


def _write_availability(cur, record_id: int, availability: str,
                        detail: dict[str, Any], *, source: str) -> None:
    """写可达轴。**一个字都不碰核实轴** —— 这正是它存在的理由。

    🔴 R4 §B:`source` 是**必填关键字参数**,不是可选留痕。
       R3 把「探针不能自己判 retracted」写成了调用点里的一句断言,而 R3 自己给出的
       理由是"调用点会新增,靠自觉必漏一个" —— 可达轴当时恰恰犯了这条。
       现在与核实轴同构:收口处校验 + 库里 `publish_records_retracted_requires_
       human_source` 兜底,两道都在。
    """
    assert availability in AVAILABILITY_STATES, f"未知可达态:{availability}"
    assert source in VERIFICATION_SOURCES, f"未知权威来源:{source}"
    assert (availability not in AVAILABILITY_HUMAN_ONLY_STATES
            or source == SOURCE_HUMAN_ATTESTATION), (
        f"{availability} 只能由人工登记下结论,不能由 {source} 签发")
    cur.execute(
        """
        UPDATE publish_records
           SET public_url_availability_state      = %s,
               public_url_availability_source     = %s,
               public_url_availability_checked_at = CURRENT_TIMESTAMP,
               public_url_availability_detail     = %s
         WHERE id = %s
        """,
        (availability, source, Json(detail), int(record_id)),
    )


def _append_audit(cur, record_id: int, *, actor_user_id: int | None, actor_kind: str,
                  action: str, from_state: str | None, to_state: str,
                  source: str, evidence_sha256: str | None = None,
                  evidence_note: str | None = None,
                  detail: dict[str, Any] | None = None) -> None:
    cur.execute(
        """
        INSERT INTO publish_record_verification_events
        (publish_record_id, actor_user_id, actor_kind, action, from_state, to_state,
         verification_source, evidence_sha256, evidence_note, detail)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (int(record_id), actor_user_id, actor_kind, action, from_state, to_state,
         source, evidence_sha256, evidence_note, Json(detail or {})),
    )


def _load_record(cur, record_id: int) -> dict[str, Any]:
    cur.execute(
        """
        SELECT id, status, article_id, platform, request_id, public_url,
               public_url_reported_explicitly, public_url_verification_state,
               public_url_verification_source,
               submitted_title_snapshot, submitted_content_snapshot,
               submitted_content_snapshot_hash, created_at
          FROM publish_records
         WHERE id = %s
           FOR UPDATE
        """,
        (int(record_id),),
    )
    return _row_dict(cur.fetchone())


# ===========================================================================
# 探针路径 —— 最高只能产出 content_matched
# ===========================================================================

def probe_publish_record_with_cursor(
    cur,
    record_id: int,
    *,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """跑一次服务端探针;返回 {state, reason, ...}。**永远不会返回 verified。**"""
    fetch = fetcher or _default_fetcher
    record = _load_record(cur, record_id)
    if not record:
        return {"state": None, "reason": "record_not_found"}

    from_state = record.get("public_url_verification_state")
    public_url = str(record.get("public_url") or "").strip()

    # 🔴🔴 R3 §① —— 已核实的行,重核**只更新可达轴**。
    #    这不是"跳过检查",恰恰相反:该探的照探、探到什么如实记,只是记在
    #    「页面此刻还在不在」那根轴上,而不是拿它去改写「当初发没发出去」。
    #    两个命题互不蕴含:页面 502 不代表当初没发,页面能开也不代表当初发过。
    if from_state == STATE_VERIFIED:
        return _reprobe_availability_with_cursor(cur, record, public_url, fetch)

    if str(record.get("status") or "").lower() != "success" or not public_url:
        _write_state(cur, record_id, STATE_UNVERIFIED, source=None, method=None,
                     detail={"reason": "no_verifiable_claim"})
        return {"state": STATE_UNVERIFIED, "reason": "no_verifiable_claim"}

    platform = record.get("platform")
    if not url_is_within_platform_domain(platform, public_url):
        # 🔴 R2 §①:域不在平台清单里,**连 content_matched 都不给**。
        #    自报一个我们根本不认识的域,本身就是最该有人看一眼的形态。
        detail = {"reason": "url_outside_platform_domain", "probed_url": public_url,
                  "platform": platform}
        _write_state(cur, record_id, STATE_NEEDS_ACTION, source=SOURCE_SERVER_PROBE,
                     method=PROBE_METHOD, detail=detail)
        _append_audit(cur, record_id, actor_user_id=None, actor_kind="system",
                      action="server_probe", from_state=from_state,
                      to_state=STATE_NEEDS_ACTION, source=SOURCE_SERVER_PROBE,
                      detail=detail)
        logger.warning("自报 URL 的域不在平台清单(待人工处理) record=%s platform=%s url=%s",
                       record_id, platform, public_url)
        return {"state": STATE_NEEDS_ACTION, **detail}

    try:
        fetched = fetch(public_url)
    except Exception as exc:
        detail = {"reason": "probe_failed",
                  "error": f"{type(exc).__name__}: {exc}"[:400],
                  "probed_url": public_url}
        # 🔴 探不动 ≠ 可以放行,也 ≠ 可以当"没发过"。落 needs_action + 出警告。
        _write_state(cur, record_id, STATE_NEEDS_ACTION, source=SOURCE_SERVER_PROBE,
                     method=PROBE_METHOD, detail=detail)
        _append_audit(cur, record_id, actor_user_id=None, actor_kind="system",
                      action="server_probe", from_state=from_state,
                      to_state=STATE_NEEDS_ACTION, source=SOURCE_SERVER_PROBE,
                      detail=detail)
        logger.warning("公开 URL 探测失败(待人工处理) record=%s url=%s err=%s",
                       record_id, public_url, detail["error"])
        return {"state": STATE_NEEDS_ACTION, **detail}

    verdict = _probe_verdict(record, fetched)
    assert verdict["state"] in PROBE_REACHABLE_STATES, "探针够不到 verified"
    detail = {
        "reason": verdict["reason"],
        "http_status": verdict.get("http_status"),
        "probed_url": public_url,
        "final_url": fetched.get("final_url"),
        "hops": fetched.get("hops"),
    }
    _write_state(cur, record_id, verdict["state"], source=SOURCE_SERVER_PROBE,
                 method=PROBE_METHOD, detail=detail)
    _append_audit(cur, record_id, actor_user_id=None, actor_kind="system",
                  action="server_probe", from_state=from_state,
                  to_state=verdict["state"], source=SOURCE_SERVER_PROBE, detail=detail)
    if verdict["state"] == STATE_NEEDS_ACTION:
        logger.warning("公开 URL 未通过内容指纹(待人工处理) record=%s url=%s reason=%s",
                       record_id, public_url, verdict["reason"])
    # 🔴 这里**没有**升终态事实的调用。content_matched 不落快照、不进 outbox。
    return {"state": verdict["state"], **detail}


# ===========================================================================
# 可达轴 —— 已核实记录的重核落点(R3 §①)
# ===========================================================================

def _reprobe_availability_with_cursor(cur, record: dict[str, Any], public_url: str,
                                      fetch: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """对已 `verified` 的行重核:探针结果落**可达轴**,核实轴一个字不改。"""
    record_id = int(record["id"])
    if not public_url:
        return {"state": STATE_VERIFIED, "axis": "availability",
                "availability": None, "reason": "no_url_to_check"}

    platform = record.get("platform")
    if not url_is_within_platform_domain(platform, public_url):
        verdict_reason = "url_outside_platform_domain"
        detail: dict[str, Any] = {"reason": verdict_reason, "probed_url": public_url,
                                  "platform": platform}
    else:
        try:
            fetched = fetch(public_url)
        except Exception as exc:
            verdict_reason = "probe_failed"
            detail = {"reason": verdict_reason, "probed_url": public_url,
                      "error": f"{type(exc).__name__}: {exc}"[:400]}
        else:
            verdict = _probe_verdict(record, fetched)
            verdict_reason = verdict["reason"]
            detail = {"reason": verdict_reason, "probed_url": public_url,
                      "http_status": verdict.get("http_status"),
                      "final_url": fetched.get("final_url"), "hops": fetched.get("hops")}

    availability = _PROBE_REASON_TO_AVAILABILITY.get(verdict_reason,
                                                     AVAILABILITY_UNREACHABLE)
    # 🔴 探针够不到 `retracted`:它看到 404 只能说"探不动",而"这篇被下架了 /
    #    客户要求撤回"是**人**才能下的结论。与「探针够不到 verified」同一条道理。
    #    R4 §B 后这条不再只写在这里 —— 传 `source=server_probe` 进收口,
    #    收口与库各拦一道,这里留着是为了报错报在最靠近现场的位置。
    assert availability not in AVAILABILITY_HUMAN_ONLY_STATES, (
        f"探针不能自行判定 {availability} —— 那是人工登记的结论")
    _write_availability(cur, record_id, availability, detail,
                        source=SOURCE_SERVER_PROBE)
    _append_audit(cur, record_id, actor_user_id=None, actor_kind="system",
                  action="availability_probe", from_state=STATE_VERIFIED,
                  to_state=STATE_VERIFIED, source=SOURCE_SERVER_PROBE,
                  detail={**detail, "availability": availability})
    if availability != AVAILABILITY_AVAILABLE:
        # 页面确实出问题了要有人知道 —— 但这是**运营信息**,不是"当初没发布"。
        logger.warning("已核实记录的页面此刻不可用 record=%s availability=%s reason=%s"
                       "(核实态保持 verified 不变)",
                       record_id, availability, verdict_reason)
    return {"state": STATE_VERIFIED, "axis": "availability",
            "availability": availability, **detail}


def record_availability_attestation_with_cursor(
    cur,
    record_id: int,
    *,
    actor_user_id: int,
    availability: str,
    evidence: str,
    note: str | None = None,
) -> dict[str, Any]:
    """人工登记可达轴(下架 / 撤回 / 已恢复)。**同样不动核实轴。**

    `retracted` 只有这条路能产生:探针看到 404 只能说"探不动",
    "这篇被平台下架了 / 客户要求撤回" 是**人**才能下的结论。
    """
    if not actor_user_id:
        raise ValueError("人工登记可达状态必须有 actor")
    if availability not in AVAILABILITY_STATES:
        raise ValueError(f"未知可达状态:{availability}")
    evidence_text = str(evidence or "").strip()
    if len(evidence_text) < 16:
        raise ValueError("证据内容过短,无法作为登记留痕")

    record = _load_record(cur, record_id)
    if not record:
        return {"state": None, "reason": "record_not_found"}
    current_state = record.get("public_url_verification_state")
    evidence_sha256 = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()
    detail = {"reason": "human_availability_attestation",
              "availability": availability, "evidence_sha256": evidence_sha256}
    _write_availability(cur, record_id, availability, detail,
                        source=SOURCE_HUMAN_ATTESTATION)
    _append_audit(cur, record_id, actor_user_id=int(actor_user_id), actor_kind="human",
                  action="availability_attestation", from_state=current_state,
                  # 🔴 to_state 写的是**核实轴现值**(没变),不是可达值 ——
                  #    审计表的 to_state 说的是核实轴,别把两根轴串到一列里。
                  to_state=str(current_state or STATE_UNVERIFIED),
                  source=SOURCE_HUMAN_ATTESTATION,
                  evidence_sha256=evidence_sha256,
                  evidence_note=((note or None) and note[:2000]), detail=detail)
    return {"state": current_state, "axis": "availability",
            "availability": availability, "evidence_sha256": evidence_sha256}


# ===========================================================================
# 人工核实动作 —— needs_action / content_matched 的出路,带 actor + 证据 + 审计
# ===========================================================================

def attest_publish_record_with_cursor(
    cur,
    record_id: int,
    *,
    actor_user_id: int,
    evidence: str,
    note: str | None = None,
) -> dict[str, Any]:
    """人工核实通过 → 升 `verified`(唯一对自助发布可用的权威路径)。

    `evidence` 是核实者留下的证据原文(截图 base64 / 归档正文 / 后台回执粘贴)。
    只存它的 sha256 与备注 —— 存原文会把客户正文与截图搬进审计表,不必要地扩大面。
    """
    if not actor_user_id:
        raise ValueError("人工核实必须有 actor")
    evidence_text = str(evidence or "").strip()
    if len(evidence_text) < 16:
        # 🔴 "留了个空字符串当证据"= 没有证据。宁可拒绝,不要一条形同虚设的审计。
        raise ValueError("证据内容过短,无法作为核实留痕")

    record = _load_record(cur, record_id)
    if not record:
        return {"state": None, "reason": "record_not_found"}
    from_state = record.get("public_url_verification_state")
    public_url = str(record.get("public_url") or "").strip()
    if str(record.get("status") or "").lower() != "success" or not public_url:
        return {"state": from_state, "reason": "no_verifiable_claim"}
    if from_state == STATE_VERIFIED:
        # 已经是终态了,重复核实是幂等的:不重写 verified_at、不重复升级发布事实。
        # (再跑一遍 `_promote_verified_fact` 只会多一条 outbox,没有任何新信息。)
        return {"state": STATE_VERIFIED, "reason": "already_verified",
                "promotion": {"captured": False, "reason": "already_verified"}}

    evidence_sha256 = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()
    detail = {"reason": "human_attested", "probed_url": public_url,
              "evidence_sha256": evidence_sha256}
    _write_state(cur, record_id, STATE_VERIFIED, source=SOURCE_HUMAN_ATTESTATION,
                 method="human_attestation_v1", detail=detail)
    _append_audit(cur, record_id, actor_user_id=int(actor_user_id), actor_kind="human",
                  action="human_attestation", from_state=from_state,
                  to_state=STATE_VERIFIED, source=SOURCE_HUMAN_ATTESTATION,
                  evidence_sha256=evidence_sha256, evidence_note=(note or None)[:2000]
                  if note else None,
                  detail=detail)
    promoted = _promote_verified_fact_with_cursor(cur, record, public_url)
    return {"state": STATE_VERIFIED, **detail, "promotion": promoted}


# ===========================================================================
# 升终态事实 —— 只在 verified 之后
# ===========================================================================

def _promote_verified_fact_with_cursor(cur, record: dict[str, Any],
                                       public_url: str) -> dict[str, Any]:
    """发布快照 + 交付槽血缘。**只有 verified 才走到这里。**

    原先这两步挂在 WS 写入方(浏览器一说 success 就落),R1 搬到探针之后,
    R2 再往后搬到权威核实之后 —— 因为探针本身不是权威。
    """
    article_id = record.get("article_id")
    if not article_id:
        return {"captured": False, "reason": "record_not_linked_to_article"}

    from services.article_publication_snapshot import (
        capture_publication_snapshot_with_cursor,
    )

    snapshot = capture_publication_snapshot_with_cursor(
        cur,
        article_id=int(article_id),
        source="publish_records",
        source_id=int(record["id"]),
        success_state="published",
        title_override=record.get("submitted_title_snapshot"),
        content_override=record.get("submitted_content_snapshot"),
        body_observation_level="extension_channel_submission_snapshot",
    )
    if not snapshot.get("content_hash"):
        return {"captured": False,
                "reason": snapshot.get("reason") or "snapshot_incomplete"}

    from services.article_delivery_plan import (
        enqueue_publication_locked_in_transaction_if_enabled,
    )

    enqueued = enqueue_publication_locked_in_transaction_if_enabled(
        cur,
        publication_source="publish_records",
        publication_source_id=int(record["id"]),
        article_id=int(article_id),
        platform=str(record.get("platform") or ""),
        provider_receipt=str(record.get("request_id") or record["id"]),
        public_url=public_url,
        submitted_content_hash=str(snapshot["content_hash"]),
        published_at=record.get("created_at"),
    )
    return {"captured": True, "snapshot_hash": snapshot.get("content_hash"),
            "outbox": enqueued}


# ===========================================================================
# 清扫
# ===========================================================================

def sweep_pending_public_urls(
    limit: int = 50,
    fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把 `pending` 队列跑一遍。逐态计数原样报出来 —— needs_action **不吞**。"""
    from db.connection import get_connection

    conn = get_connection()
    counts: dict[str, int] = {STATE_CONTENT_MATCHED: 0, STATE_NEEDS_ACTION: 0,
                              STATE_UNVERIFIED: 0}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM publish_records
             WHERE public_url_verification_state = %s
             ORDER BY created_at ASC
             LIMIT %s
            """,
            (STATE_PENDING, int(limit)),
        )
        ids = [int(_row_dict(r)["id"]) for r in cur.fetchall()]
        for record_id in ids:
            try:
                result = probe_publish_record_with_cursor(cur, record_id, fetcher=fetcher)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("探测单条时异常 record=%s", record_id)
                continue
            state = result.get("state")
            if state in counts:
                counts[state] += 1
        actionable = counts[STATE_NEEDS_ACTION] + counts[STATE_CONTENT_MATCHED]
        if actionable:
            logger.warning("公开 URL 探测清扫:%s 条待人工核实(content_matched %s / "
                           "needs_action %s)—— 二者都**不是**已发布",
                           actionable, counts[STATE_CONTENT_MATCHED],
                           counts[STATE_NEEDS_ACTION])
        return {"scanned": len(ids), **counts}
    finally:
        conn.close()
