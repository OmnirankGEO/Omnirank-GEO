"""
services/marketing/image_client.py — GPT-Image-2 生成客户端(apimart 聚合 · 开发文档 §1)

接入规格(实测定型):
  POST https://api.apimart.ai/v1/images/generations
    body: model='gpt-image-2'(固定) · prompt · n · size(比例/像素) · resolution(1k/2k/4k) ·
          official_fallback=true(生产建议)· image_urls(可选垫图)
    resp: data[0].task_id
  轮询 GET https://api.apimart.ai/v1/tasks/{task_id} → 完成后 data.result.images[0].url[0]
  实测:提交 ~2s,出图 ~63s(1k),cost ~$0.01/张(1k);(示例值)429=限频退避。

工程约定(§1 / §D.2):
  · KEY 只走 env(APIMART_API_KEY / MARKETING_APIMART_KEY)· 代码零硬编码 · 本文档密钥不入 git。
  · 全程 httpx + llm_track(平台 apimart · 成本遥测,非扣费)· fail-soft 返回 None 类。
  · _submit / _poll 是可 monkeypatch 的私有 seam(测试注入,不打真实 API)。
"""
import asyncio
import inspect
import io
import ipaddress
import logging
import os
import socket
from typing import Awaitable, Callable, Optional
from urllib.parse import quote, urlparse

logger = logging.getLogger("GEO-Marketing-Image")

APIMART_GEN_URL = "https://api.apimart.ai/v1/images/generations"
APIMART_TASK_URL = "https://api.apimart.ai/v1/tasks/{task_id}"
MODEL = "gpt-image-2"


class LiveAuthorityRejected(RuntimeError):
    """The local live-authority/lease check failed before network I/O.

    🔴 [#113] 带上**底层 code**。原来只 `LiveAuthorityRejected(str(exc))`,
       code 在这一跳就丢了,上游只能无差别归类 —— 于是「租约被别人抢走」
       被贴成「品牌授权已撤销」,用户看到一句**具体但错误**的话。
       笼统的话让人继续找,错的具体让人停止找并走错方向。
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code

_SUBMIT_TIMEOUT_S = 20.0
_POLL_TIMEOUT_S = 20.0

# ─────────────────────────────────────────────────────────────
# 轮询策略(2026-08-02 按真实响应字段重定)
# ─────────────────────────────────────────────────────────────
# 生产实测 provider 的 task 响应带这些字段(不是文档示例,是真调出来的):
#     {"status": "processing", "progress": 50,
#      "estimated_time": 100, "actual_time": 49, "cost": 0.01, ...}
#
# 🔴 改动一:上限 150 → 420。实测同一 prompt 快的 49s、慢的 >240s(差 5 倍),
#    150s 直接把慢的那批判死 —— A/B 实验 6 张里 2 张(33%)就是这么报废的,
#    且 `丢拍 0 次`,纯粹是超时不是网络。
# 🔴 改动二:首轮延迟。既然 provider 自己给 estimated_time,
#    从第 3 秒起每 3 秒盲问一次是纯浪费:一个预计 100s 的任务,
#    前 90 秒的问答全是白问,而 RPM 额度是有限的(500)。
#    改成"先睡 estimated_time 的一部分,再开始问"。
# 🔴 改动三:间隔 3 → 5 秒。配合首轮延迟,每张在飞图的请求从 ~20/min 降到 ~5/min,
#    同样的 500 RPM 能支撑的并发数从 ~25 张升到 ~90 张。
_POLL_INTERVAL_S = 5.0
_POLL_MAX_WAIT_S = 420.0
# 首轮延迟 = estimated_time × 本系数,再夹在 [下限, 上限] 之间。
# 取 0.6 而不是 1.0:实测 actual 可能只有 estimated 的一半(49 vs 100),
# 睡满预估会白等;睡六成兼顾"少问"与"别错过早完成"。
_POLL_FIRST_DELAY_RATIO = 0.6
_POLL_FIRST_DELAY_MIN_S = 8.0
_POLL_FIRST_DELAY_MAX_S = 90.0


def _first_poll_delay(estimated_time: Optional[float]) -> float:
    """首轮轮询前先睡多久。provider 没给 estimated_time 时退回下限(照旧尽快问)。"""
    try:
        est = float(estimated_time or 0)
    except (TypeError, ValueError):
        est = 0.0
    if est <= 0:
        return _POLL_FIRST_DELAY_MIN_S
    return max(_POLL_FIRST_DELAY_MIN_S,
               min(est * _POLL_FIRST_DELAY_RATIO, _POLL_FIRST_DELAY_MAX_S))

# ─────────────────────────────────────────────────────────────
# 连接失败重试(返工单 2026-08-02)
# ─────────────────────────────────────────────────────────────
# 背景(生产实测,非推断):出海梯子(SOCKS5)握手成功率在 42%~75% 之间飘。
# 一条图文要生 N 张卡、每张一次独立调用 → 全成功率 = p^N;
# 按 42% 算 6 张卡只有 0.5%。零重试 = 开闸即基本出不了成品,且每条失败都要退款。
#
# 🔴🔴 计费安全边界 —— 逐字照 submit_image 的 docstring 执行,**不另立口径**:
#     "Connect failures are the only safe no-send class."
#   ✅ 可重试:ConnectError / ConnectTimeout / ProxyError —— 请求根本没送出去,不可能被计费
#   ❌ 绝不可重试:ReadTimeout / RemoteProtocolError / 任何已建连后的失败
#      —— provider 可能已受理,重发 = 第二次付费
#   ❌ 收到**任何 HTTP 响应**(含 4xx/5xx)= 请求已送达,不在重试类
#      (`_submit` 非 200 时返回 None,那是 submit_failed,不走重试)
_RETRY_BASE_S = 1.5
_RETRY_MAX_S = 12.0


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.environ.get(name, "")).strip() or default))
    except (TypeError, ValueError):
        return default


def connect_retries() -> int:
    """submit 跳的 connect 类重试次数。

    🔴 单一来源 + env 可覆盖 —— 返工单明确禁止把次数写死在多处
       (梯子质量变了要能只调一个旋钮)。
    """
    return _int_env("MARKETING_IMAGE_CONNECT_RETRIES", 5)


def download_retries() -> int:
    """下载跳重试次数。下载是**免费幂等 GET**,重试类可放宽,次数也可比 submit 少。"""
    return _int_env("MARKETING_IMAGE_DOWNLOAD_RETRIES", 4)


def _backoff_delay(attempt: int) -> float:
    """第 attempt 次失败后的等待秒数。指数退避 + jitter,跨度秒~十秒级。

    🔴 必须带 jitter 且跨度拉开:梯子抖动是**突发相关**的 ——
       5 连击若全打在同一个抖动窗口里,等于没重试(返工单 §6.5)。
       full-jitter 变体:保底 50%、上浮到满值,让并发的多张卡不会同拍重试。
    """
    import random
    ceiling = min(_RETRY_BASE_S * (2 ** max(0, attempt - 1)), _RETRY_MAX_S)
    return ceiling * (0.5 + random.random() * 0.5)


def _connect_error_types() -> tuple:
    """**唯一**的"没送出去"异常集合。改这里 = 改计费安全边界,改前先读上面那段。"""
    import httpx
    return (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)


def _download_retry_types() -> tuple:
    """下载跳可重试集合 = connect 类 + read 类。

    下载是免费幂等 GET,不存在重复计费,所以比 submit 跳宽。
    🔴 但 ValueError(SSRF 校验 / content-type / 过大)**不在**其中 ——
       那些是判定结果,不是网络抖动,重试只会重复失败。
    """
    import httpx
    return _connect_error_types() + (httpx.ReadTimeout, httpx.ReadError)


def _proxy_label() -> str:
    """代理的可运维标识:`协议://主机:端口`。

    🔴🔴 **绝不含 user/pass**。现役配置形如 `socks5h://user:pass@host:port`,
       原样打进日志/错误串就是把凭据写进日志与 API 响应。
       urlparse 的 .hostname/.port 天然剔除凭据段,不要自己切字符串。
    """
    raw = _get_proxy()
    if not raw:
        return "direct"
    try:
        parsed = urlparse(raw)
        host = parsed.hostname or "?"
        port = f":{parsed.port}" if parsed.port else ""
        scheme = parsed.scheme or "?"
        return f"{scheme}://{host}{port}"
    except Exception:  # noqa: BLE001 - 代理串畸形不该让生图整条挂掉
        return "unparsable"


def _conn_detail(exc: BaseException, attempt: int, total: int) -> str:
    """connect 类失败的可运维描述。

    httpx.ConnectError 的 str() 天生是空串 —— 只打 str(e) 等于什么都没说
    (生产实测:日志只有 "generate 异常(fail-soft): " 后面空白,运维无从下手)。
    所以固定带三样:异常类型 + 代理标识 + 第几次/共几次。
    """
    return f"{type(exc).__name__}(proxy={_proxy_label()}, attempt={attempt}/{total})"


class ImageConnectFailed(RuntimeError):
    """connect 类重试用尽。str() 自带类型/代理/次数,交给上层统一 fail-soft。"""


# ─────────────────────────────────────────────────────────────
# 单张成本(USD)· 仅用于毛利复盘,不参与扣费(扣费走 feature_pricing)
# ─────────────────────────────────────────────────────────────
# 🔴 全部四档已核实(Owner 2026-08-03 提供 provider「定价中心」页 · gpt-image-2):
#
#     规格    我们的价格            官方价格              节省
#     默认    0.085  Credits/张     0.10625 Credits/张    20%
#     1K      0.085  Credits/张     0.10625 Credits/张    20%
#     2K      0.14   Credits/张     0.175   Credits/张    20%
#     4K      0.21   Credits/张     0.2625  Credits/张    20%
#
#   取的是 **「我们的价格」那一列**(= 我方实际费率),不是官方列表价。
#   1k 单价与 provider 响应体自报的 `cost` 一致 → 这一列就是计费口径(开源版为示例值)。
#
# 🔴 一条方法论留痕(别删):上一版这里写的是"2k/4k 未核实,不凭 0.8 的比值外推"。
#    现在定价页证明那个 0.8 **确实**是账号折扣(四档全是 20% 节省)——
#    但当时拒绝外推仍然是对的:**猜对了不等于当时有依据**。
#    真正让它变成事实的是拿到了「我们的价格」这一列,不是那个比值。
#    (顺带:旧值两个都错 —— 2k 低估 17%、4k 高估 14%。)
#
# ⚠️ 影响面仍然很小:本仓库生图全部走 1k
#    (services/geo_douyin/image_pipeline.py 的 _IMAGE_RESOLUTION = "1k")。
IMAGE_COST_USD_1K = 0.01
IMAGE_COST_USD = {
    "1k": IMAGE_COST_USD_1K,
    "2k": 0.014,   # 定价中心 0.14 Credits/张
    "4k": 0.021,   # 定价中心 0.21 Credits/张
}


# ─────────────────────────────────────────────────────────────
# 一次提交能出几张(provider 硬约束)
# ─────────────────────────────────────────────────────────────
# 🔴 provider 文档(gpt-image-2 generation)明确:**`n` 取值只能是 1**。
#    而此前代码写的是 `max(1, min(n, 10))` —— 允许到 10。
#
# 🔴 这不只是"传大了会报错",它是**计费面的口子**:
#    提交响应里 `data` 是个数组,而本模块只取 `arr[0]` 一个 task_id。
#    所以万一 provider 某天真的接受了 n>1,我们会**按 n 张被计费、只用掉 1 张**,
#    多出来的钱一声不响地流走(没有任何日志会提到它)。
#
#    夹到 1 是**往安全侧夹**:宁可少出图,不可多付钱。
#    调用方传了 >1 会打 warning —— 静默改用户的入参本身也是一种隐瞒。
_PROVIDER_MAX_N = 1


def _clamp_n(n: int) -> int:
    """把 n 夹到 provider 真正接受的范围。>1 时留痕,不静默。"""
    try:
        want = int(n)
    except (TypeError, ValueError):
        want = 1
    safe = max(1, min(want, _PROVIDER_MAX_N))
    if want != safe:
        logger.warning(
            "[image] n=%s 超出 provider 上限 %s,已夹到 %s。"
            "(提交响应只取第 1 个 task_id,放行 n>1 等于按 n 张计费只用 1 张)",
            want, _PROVIDER_MAX_N, safe)
    return safe


def _get_key() -> str:
    return (os.environ.get("APIMART_API_KEY")
            or os.environ.get("MARKETING_APIMART_KEY") or "").strip()


def _get_proxy() -> Optional[str]:
    """生图出海通道(老板 2026-07-06 拍板:复用服务器现有梯子)。

    prod 到 api.apimart.ai 被 DNS 污染 + 连接层阻断(实测钉真实 IP 也不通),
    而 Jina 线的 socks 梯子实测可达(经梯 404/401=API 正常应答)。
    优先 APIMART_HTTP_PROXY(专用覆盖),回落 JINA_HTTP_PROXY(prod 容器现成有,零配置);
    都未配 → None 直连(本机开发网络可直达)。姿势照 services/research_monitor/crawler.py P14.6。
    """
    return (os.environ.get("APIMART_HTTP_PROXY")
            or os.environ.get("JINA_HTTP_PROXY") or "").strip() or None


def _generation_url() -> str:
    return os.environ.get("MARKETING_IMAGE_GENERATION_URL", APIMART_GEN_URL).strip()


def _task_url(task_id: str) -> str:
    template = os.environ.get("MARKETING_IMAGE_TASK_URL", APIMART_TASK_URL).strip()
    return template.format(task_id=quote(str(task_id), safe="-_"))


async def _guard(callback: Optional[Callable[[], object]]) -> None:
    if callback is None:
        return
    try:
        result = callback()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        raise LiveAuthorityRejected(
            str(exc), code=getattr(exc, "code", None)) from exc


async def _submit(prompt: str, size: str, resolution: str, n: int,
                  image_urls: Optional[list] = None) -> Optional[str]:
    """提交生成任务 → 返回 provider task_id(seam · 测试 monkeypatch)。"""
    key = _get_key()
    if not key:
        return None
    import httpx
    body = {
        "model": MODEL, "prompt": prompt, "n": _clamp_n(n),
        "size": size, "resolution": resolution, "official_fallback": True,
    }
    if image_urls:
        body["image_urls"] = image_urls[:16]
    async with httpx.AsyncClient(timeout=_SUBMIT_TIMEOUT_S, proxy=_get_proxy()) as client:
        resp = await client.post(_generation_url(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"},
                                 json=body)
        if resp.status_code != 200:
            logger.warning("[image] submit HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        arr = data.get("data") or []
        return arr[0].get("task_id") if arr else None


async def _submit_with_retry(prompt: str, size: str, resolution: str, n: int,
                            image_urls: Optional[list] = None) -> Optional[str]:
    """submit 跳的 connect 类重试外壳。

    🔴 只在**抛出 connect 类异常**时重试 —— 那是唯一"请求没送出去"的证据。
    🔴 `_submit` 返回 None(非 200 / 无 task_id)属于**已收到响应**,
       是 submit_failed,**原样返回不重试**(重发 = 第二次付费)。
       返工单 §6.3 专门澄清过这条,防止实现时把"响应错误"混进"连接失败"。
    🔴 `_submit` 仍是原来那个 seam:本函数按名调用它,
       测试 monkeypatch `_submit` 时能如实数到调用次数。
    """
    total = connect_retries()
    last_detail = ""
    for attempt in range(1, total + 1):
        try:
            return await _submit(prompt, size, resolution, n, image_urls)
        except _connect_error_types() as exc:
            last_detail = _conn_detail(exc, attempt, total)
            logger.warning("[image] submit 连接失败,重试中: %s", last_detail)
            if attempt < total:
                await asyncio.sleep(_backoff_delay(attempt))
    raise ImageConnectFailed(last_detail or f"connect_failed(attempts={total})")


async def _poll(task_id: str, *,
                on_progress: Optional[Callable[[dict], object]] = None) -> Optional[str]:
    """轮询任务 → 返回成品图 URL(seam · 测试 monkeypatch)。

    on_progress 每拿到一次 200 就回调一次,入参是 provider 的真实进度快照:
        {"progress": 0-100, "status": "...", "estimated_time": s,
         "actual_time": s|None, "waited": s}
    🔴 用它做前端进度**不需要模拟** —— provider 自己给 progress 和 estimated_time。
       模拟一个固定秒数的倒计时反而更糟:实测同一 prompt 快的 49s、慢的 >240s,
       固定倒计时归零后任务还在跑,用户会当成卡死。
    """
    key = _get_key()
    if not key:
        return None
    import httpx
    waited = 0.0
    misses = 0
    first_delay_done = False

    def _emit(payload: dict) -> None:
        if on_progress is None:
            return
        try:
            on_progress(payload)
        except Exception as e:  # noqa: BLE001 - 进度回调炸了不该拖垮生图
            logger.warning("[image] 进度回调异常(已忽略): %s",
                           f"{type(e).__name__}: {e}".rstrip(": "))

    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT_S, proxy=_get_proxy()) as client:
        while waited < _POLL_MAX_WAIT_S:
            try:
                resp = await client.get(_task_url(task_id),
                                        headers={"Authorization": f"Bearer {key}"})
            except _connect_error_types() as exc:
                # 🔴 轮询跳的韧性(返工单 §6.1):走到这里时 submit **已经成功 = 已计费**。
                #    一次瞬时 connect 抖动就抛出去,等于把付过钱的任务丢掉
                #    (平台白花钱 + 用户还要退款)。所以视为**丢一拍**:
                #    sleep + continue,并**计入 _POLL_MAX_WAIT_S 预算**(不会无限轮询)。
                # 🔴 这是补韧性,不是重新提交 —— 本函数任何路径都不会再发一次 POST。
                misses += 1
                logger.warning("[image] task %s 轮询丢拍(第 %d 次): %s",
                               task_id, misses, _conn_detail(exc, misses, misses))
                await asyncio.sleep(_POLL_INTERVAL_S)
                waited += _POLL_INTERVAL_S
                continue
            if resp.status_code == 429:
                await asyncio.sleep(_POLL_INTERVAL_S * 2)
                waited += _POLL_INTERVAL_S * 2
                continue
            if resp.status_code == 200:
                data = resp.json()
                payload = data.get("data") or {}
                result = payload.get("result") or {}
                images = result.get("images") or []
                status = payload.get("status") or data.get("status")
                # 真实进度快照 —— 前端的进度条/预估剩余全部来自这里,不需要模拟
                _emit({"progress": payload.get("progress"),
                       "status": status,
                       "estimated_time": payload.get("estimated_time"),
                       "actual_time": payload.get("actual_time"),
                       "waited": round(waited, 1)})
                if images:
                    url0 = images[0].get("url")
                    if isinstance(url0, list):
                        url0 = url0[0] if url0 else None
                    if url0:
                        return url0
                if status in ("failed", "error"):
                    logger.warning("[image] task %s failed", task_id)
                    return None
                # 🔴 首轮之后才按 estimated_time 做一次长睡:
                #    provider 说要 100s,就没必要从第 5 秒起每 5 秒问一次。
                #    只做一次,之后回到固定间隔 —— 免得预估不准时越睡越久。
                if not first_delay_done:
                    first_delay_done = True
                    lead = _first_poll_delay(payload.get("estimated_time"))
                    if lead > _POLL_INTERVAL_S:
                        await asyncio.sleep(lead)
                        waited += lead
                        continue
            await asyncio.sleep(_POLL_INTERVAL_S)
            waited += _POLL_INTERVAL_S
    # 放弃时保留 task_id(返工单要求):这条任务已计费,留着才能事后人工捞回
    logger.warning("[image] task %s 轮询超时(其中丢拍 %d 次,代理 %s)",
                   task_id, misses, _proxy_label())
    return None


async def generate_image(prompt: str, *, size: str = "3:4", resolution: str = "1k",
                         n: int = 1, image_urls: Optional[list] = None) -> dict:
    """生成一张图。返回 {ok, image_url?, provider_task_id?, cost_usd, error?}。fail-soft。"""
    key = _get_key()
    if not key:
        return {"ok": False, "error": "no_apimart_key", "cost_usd": 0.0}
    try:
        from tools.llm_call_tracker import llm_track
        async with llm_track("material_factory_image", "apimart", model=MODEL) as tracker:
            # connect 类重试在 _submit_with_retry 里;非 200 / 无 task_id 不重试
            task_id = await _submit_with_retry(prompt, size, resolution, n, image_urls)
            if not task_id:
                tracker.record(success=False, error_msg="submit_failed")
                return {"ok": False, "error": "submit_failed", "cost_usd": 0.0}
            url = await _poll(task_id)
            if not url:
                tracker.record(success=False, error_msg="poll_failed")
                return {"ok": False, "error": "poll_failed", "provider_task_id": task_id, "cost_usd": 0.0}
            # apimart 计费口径以后台为准;此处成本估用于毛利复盘
            cost_usd = IMAGE_COST_USD.get(resolution, IMAGE_COST_USD["1k"])
            tracker.record(input_tokens=0, output_tokens=0, success=True)
            return {"ok": True, "image_url": url, "provider_task_id": task_id, "cost_usd": cost_usd}
    except Exception as e:  # noqa: BLE001
        # 🔴 诊断信号必须带**异常类型**:不少异常的 str() 是空串
        #    (asyncio.TimeoutError / httpx 的部分传输类异常都是),
        #    只记 str(e) 会打出 "generate 异常(fail-soft): " 后面什么都没有,
        #    调用方拿到的 error 也是空串 —— 出了事完全无从下手。
        #    2026-08-02 实测:table_review 那张连挂两次,日志和 error 全空,
        #    白跑两次付费生图仍不知道原因。fail-soft 可以,失声不行。
        detail = f"{type(e).__name__}: {e}".rstrip(": ").strip()
        logger.warning("[image] generate 异常(fail-soft): %s", detail)
        return {"ok": False, "error": detail[:120], "cost_usd": 0.0}


async def submit_image(prompt: str, *, size: str = "3:4", resolution: str = "1k",
                       n: int = 1, image_urls: Optional[list] = None,
                       before_request: Optional[Callable[[], Awaitable[None] | None]] = None) -> dict:
    """Submit exactly once and classify whether another paid POST is safe.

    A read timeout/protocol error is outcome-unknown: the provider may have
    accepted the request, so callers must persist that state and never submit a
    replacement automatically.  Connect failures are the only safe no-send
    class.
    """
    if not _get_key():
        return {"state": "rejected", "error": "no_apimart_key"}
    import httpx
    body = {
        "model": MODEL, "prompt": prompt, "n": _clamp_n(n),
        "size": size, "resolution": resolution, "official_fallback": True,
    }
    if image_urls:
        body["image_urls"] = image_urls[:16]
    try:
        await _guard(before_request)
        async with httpx.AsyncClient(timeout=_SUBMIT_TIMEOUT_S, proxy=_get_proxy()) as client:
            response = await client.post(
                _generation_url(),
                headers={"Authorization": f"Bearer {_get_key()}", "Content-Type": "application/json"},
                json=body,
            )
        if response.status_code != 200:
            if 400 <= response.status_code < 500:
                return {"state": "rejected", "error": f"submit_http_{response.status_code}"}
            return {"state": "outcome_unknown", "error": f"submit_http_{response.status_code}"}
        data = response.json()
        rows = data.get("data") or []
        task_id = str(rows[0].get("task_id") or "") if rows else ""
        if not task_id:
            # A syntactically successful response without a recovery handle
            # does not prove the paid request was rejected. Quarantine it as
            # outcome-unknown so no second POST can be issued automatically.
            return {"state": "outcome_unknown", "error": "submit_missing_task_id"}
        return {"state": "submitted", "provider_task_id": task_id}
    except LiveAuthorityRejected:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        return {"state": "not_sent", "error": type(exc).__name__}
    except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError) as exc:
        return {"state": "outcome_unknown", "error": type(exc).__name__}
    except Exception as exc:  # invalid JSON is a received response, not safe to retry
        return {"state": "outcome_unknown", "error": type(exc).__name__}


async def poll_image_task(task_id: str, *,
                          before_request: Optional[Callable[[], Awaitable[None] | None]] = None,
                          max_wait_s: Optional[float] = None) -> dict:
    """Poll a known provider task. Pending/timeout is resumable without POST."""
    if not _get_key() or not task_id:
        return {"state": "failed", "error": "missing_poll_credentials"}
    import httpx
    waited = 0.0
    limit = _POLL_MAX_WAIT_S if max_wait_s is None else max(0.0, float(max_wait_s))
    try:
        async with httpx.AsyncClient(timeout=_POLL_TIMEOUT_S, proxy=_get_proxy()) as client:
            while waited <= limit:
                await _guard(before_request)
                response = await client.get(
                    _task_url(task_id), headers={"Authorization": f"Bearer {_get_key()}"},
                )
                if response.status_code == 200:
                    data = response.json()
                    payload = data.get("data") or {}
                    status = str(payload.get("status") or data.get("status") or "").lower()
                    images = (payload.get("result") or {}).get("images") or []
                    if images:
                        url = images[0].get("url")
                        if isinstance(url, list):
                            url = url[0] if url else ""
                        if url:
                            return {"state": "succeeded", "image_url": str(url)}
                    if status in {"failed", "error", "cancelled"}:
                        return {"state": "failed", "error": f"provider_{status}"}
                elif response.status_code not in {404, 408, 425, 429, 500, 502, 503, 504}:
                    return {"state": "failed", "error": f"poll_http_{response.status_code}"}
                if waited >= limit:
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)
                waited += _POLL_INTERVAL_S
    except LiveAuthorityRejected:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return {"state": "pending", "error": type(exc).__name__}
    except Exception as exc:
        return {"state": "pending", "error": type(exc).__name__}
    return {"state": "pending", "error": "poll_timeout"}


def _download_hosts() -> set[str]:
    raw = os.environ.get("MARKETING_IMAGE_DOWNLOAD_HOSTS", "api.apimart.ai,cdn.apimart.ai")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    allow_http = os.environ.get("MARKETING_IMAGE_ALLOW_HTTP", "").lower() == "true"
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}) or not parsed.hostname:
        raise ValueError("image_url_scheme_forbidden")
    host = parsed.hostname.lower()
    if host not in _download_hosts():
        raise ValueError("image_url_host_forbidden")
    allow_private = {
        item.strip().lower() for item in os.environ.get("MARKETING_IMAGE_ALLOW_PRIVATE_HOSTS", "").split(",") if item.strip()
    }
    if host not in allow_private:
        for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80)):
            address = ipaddress.ip_address(info[4][0])
            if not address.is_global:
                raise ValueError("image_url_address_forbidden")


async def download_image(url: str) -> Optional[bytes]:
    """下载成品图。免费幂等 GET → 网络类失败可重试(返工单 §6.2)。

    🔴 每次重试都会重新走 `_download_once` → 里面**逐跳重跑 `_validate_download_url`**,
       不存在"第一次校验过了后面就免检"的绕过口子。
    🔴 只重试网络类(connect + read);SSRF 校验失败 / content-type 不合法 / 超大
       这些是**判定结果**,重试只会重复失败,直接放弃。
    """
    total = download_retries()
    for attempt in range(1, total + 1):
        try:
            return await _download_once(url)
        except _download_retry_types() as exc:
            detail = _conn_detail(exc, attempt, total)
            logger.warning("[image] download 连接失败,重试中: %s", detail)
            if attempt < total:
                await asyncio.sleep(_backoff_delay(attempt))
            else:
                logger.warning("[image] download 放弃: %s", detail)
        except Exception as e:  # noqa: BLE001 - 非网络类:判定结果,不重试
            logger.warning("[image] download 失败: %s",
                           f"{type(e).__name__}: {e}".rstrip(": ").strip())
            return None
    return None


async def _download_once(url: str) -> Optional[bytes]:
    """Strict bounded image fetch with per-redirect SSRF validation.

    🔴 本函数**不吞异常** —— 网络类异常要抛给 `download_image` 决定是否重试。
       原来的 try/except 收口移到了外层;这里只保留业务判定。
    """
    import httpx
    current = str(url)
    async with httpx.AsyncClient(timeout=30.0, proxy=_get_proxy(),
                                 follow_redirects=False) as client:
        for _ in range(3):
            # 🔴 每一跳(含每次重定向后)都重跑 SSRF 校验,不缓存结论
            _validate_download_url(current)
            async with client.stream("GET", current) as resp:
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location") or ""
                    current = str(httpx.URL(current).join(location))
                    continue
                if resp.status_code != 200:
                    # 收到了响应 = 服务端明确答复,重试没有意义
                    return None
                content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].lower()
                if content_type not in {"image/png", "image/jpeg", "image/webp"}:
                    raise ValueError("image_content_type_forbidden")
                declared = int(resp.headers.get("content-length") or 0)
                if declared > 12 * 1024 * 1024:
                    raise ValueError("image_too_large")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 12 * 1024 * 1024:
                        raise ValueError("image_too_large")
                from PIL import Image
                with Image.open(io.BytesIO(body)) as image:
                    image.verify()
                return bytes(body)
        raise ValueError("image_redirect_limit")
