"""
Jina Reader 爬取器 + 预过滤

预过滤 4 类(优化 1 + P0-A 版权防护):
1. 域名黑名单(domain_tiering 提供)
2. 文件后缀(.pdf / .mp4 / .zip 等)
3. URL pattern (/search / /list/ / /tag/ 等明显非文章页)
4. robots.txt Disallow (P0-A 版权防护, 24h 缓存)

让 5000 引用 URL 砍到 ~3500 进 Jina 爬取。

A.4 review fix(2026-05-05):
- I2 Jina HTTP GET 加简单 retry(2 次重试 · 间隔 1s)防瞬时网络失败丢数据
"""
import concurrent.futures
import logging
import os
import threading
import time
import asyncio
import socket
import ipaddress
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from urllib.request import urlopen, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

import httpx

from services.research_monitor.domain_tiering import is_blacklisted

JINA_BASE = "https://r.jina.ai/"

# P14.6 (2026-06-01 老板): 生产服务器 (Aliyun ECS) 无法直连 r.jina.ai (出境管制 · Errno 101)
# 解决:服务器侧装 Xray (vmess client) listen 127.0.0.1:10809 · backend 通过 env 设代理
# 仅 Jina httpx Client 走代理 · 其他出站 (LLM/OSS/...) 不受影响
# 本地开发不设 env → None → 直连不变
# 部署文档: scripts/JINA_PROXY_DEPLOY.md
JINA_HTTP_PROXY = os.getenv("JINA_HTTP_PROXY", "").strip() or None

UNSUPPORTED_EXTENSIONS = (
    '.pdf', '.mp4', '.zip', '.docx', '.xlsx', '.pptx', '.tar', '.gz', '.rar',
    '.mp3', '.wav', '.avi', '.mov', '.exe', '.dmg', '.iso',
)

# URL 形状黑名单(明显不是文章页)
URL_PATH_BLACKLIST_PATTERNS = (
    '/search', '/list/', '/tag/', '/category/', '/login', '/register',
    '/cart', '/checkout', '/api/',
)


class PreFilterReason(Enum):
    BLACKLIST_DOMAIN = "blacklist_domain"
    UNSUPPORTED_EXTENSION = "unsupported_extension"
    URL_PATTERN = "url_pattern"
    HEAD_TOO_SMALL = "head_content_length_too_small"
    ROBOTS_DISALLOWED = "robots_txt_disallowed"
    # [GEO-R1-CAN-123] SSRF 防护:host 解析到内网/回环/元数据地址
    PRIVATE_ADDRESS = "private_or_internal_address"
    # [2026-07-16 轮2审核修] 网络检查超时/被弃养(慢站 tarpit · 非私网)独立原因码,
    # 不再复用 PRIVATE_ADDRESS 免 telemetry 语义失真; 语义仍是 fail-closed 过滤。
    NETWORK_CHECK_TIMEOUT = "network_check_timeout"


logger = logging.getLogger("GEO-ResearchMonitor.Crawler")

# robots.txt 缓存(24h TTL,避免每次跑批 fetch 同一域名几百次)
_ROBOTS_CACHE: Dict[str, Tuple[Optional[RobotFileParser], float]] = {}
_ROBOTS_CACHE_TTL = 24 * 3600  # 24 小时
# [2026-07-16 Stage2 并发预筛] robots 抓取超时 5s → 3s(硬要求 4);
# 超时/不可用 → parser=None → 放行, 语义与旧版一致。
# ⚠️ [出口审核 · 轮2订正] 该 3s 是 urllib per-socket-op 超时(connect/每次 recv 各 3s),
# 不是总时长上界 — 滴流(tarpit)服务器每 <3s 滴 1 字节。三层防御各管一段:
#   1) 【非 chunked】正文滴流: 用 resp.read1()(对 identity/定长响应=单次 recv,
#      ≤3s 返回)逐块读 + 每块后查 _ROBOTS_TOTAL_READ_DEADLINE_SECONDS 墙钟预算
#      → 被钉 worker 最迟 预算+3s 自行退出腾额度(超限放弃=放行)。用 read(n) 会
#      阻塞凑满 n 字节 → 墙钟检查被越过(轮2 抓到的 must_fix), 必须 read1。
#      ⚠️[轮3订正] read1 对 Transfer-Encoding: chunked 非单次 recv —— 其内部
#      _read1_chunked→readline 读 chunk-size 行会跨多次 recv, 攻击者逐字节滴
#      chunk-size 行可让单次 read1 阻塞到 _MAXLINE(~54h)。故 chunked 正文滴流
#      与头部滴流同属"worker 无法自解放"类, 不由护栏1 兜底, 由护栏3 兜底。
#   2) 头部滴流 / chunked 正文滴流: opener.open() 内读 status line/headers、或
#      read1 内读 chunk-size 行, 都在墙钟检查之外阻塞 → worker 无法自解放,
#      由护栏3 兜底(线程遗留至自然结束 = 已知 LOW 残余)。
#   3) 编排层总墙钟 _prefilter_total_deadline: wait 循环每片查, 超总预算即对
#      全部未完成 origin fail-closed 并返回 → 无论何种滴流, prefilter 保证有界
#      返回, 绝不无限期阻塞 event loop(轮2 抓到的死循环 must_fix:头部滴流域
#      钉满 max_workers 时排队 origin 永无 started_at 永不被弃养 → while pending
#      死循环)。残余: 被钉死线程 Python 无法强杀, 遗留至其自然结束(WARN 可见,
#      每轮 ≤ max_workers 个)= 已知 LOW。
#   * _ORIGIN_CHECK_ABANDON_SECONDS: 单任务弃养(在跑超龄, 快于总预算的细粒度腾额)。
_ROBOTS_FETCH_TIMEOUT_SECONDS = 3.0
_ROBOTS_TOTAL_READ_DEADLINE_SECONDS = 10.0
_ORIGIN_CHECK_ABANDON_SECONDS = 60.0
# 编排层总墙钟兜底: 按 origin 数动态(每域预算 / 并发 × 裕量), 下限/上限夹逼。
# 正常大轮(数千域 · 真实网络秒级)远低于此; 仅全 worker 被头部滴流钉死时触发。
_PREFILTER_MIN_TOTAL_DEADLINE_SECONDS = 600.0    # 10min 下限(不误杀正常轮)
_PREFILTER_MAX_TOTAL_DEADLINE_SECONDS = 3600.0   # 1h 上限(< 6h 僵尸 hard_timeout)
_PREFILTER_PER_ORIGIN_BUDGET_SECONDS = 20.0      # 每域墙钟预算(正常单域 ≤~13s)
# 判停探测(每次=一条 DB 查询)的最小间隔: 防提交循环对大轮打出 O(域数) 串行 DB 往返
_STOP_PROBE_MIN_INTERVAL_SECONDS = 2.0
# [2026-07-16 Stage2 并发预筛] 缓存线程安全化(硬要求 6):
# - _ROBOTS_CACHE_LOCK 保护 cache/in-flight 两张表的读写(条目一次性完整写入, 无半成品);
# - _ROBOTS_INFLIGHT 同域在飞去重: 并发同域请求只有第一个真的发 fetch,
#   其余 wait 同一个 Event, 完成后统一从 cache 读 → 同域 robots 绝不重复抓取。
_ROBOTS_CACHE_LOCK = threading.Lock()
_ROBOTS_INFLIGHT: Dict[str, threading.Event] = {}
# robots UA(判定与抓取共用)
ROBOTS_UA = 'GEO-ResearchMonitor'


def _resolve_host_ips(host: str) -> List[str]:
    """解析 host 的全部 A/AAAA 地址(可能抛异常,由调用方兜)。"""
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _is_safe_public_host(host: Optional[str]) -> bool:
    """
    [GEO-R1-CAN-123] SSRF 防护:判断 host 是否可安全发出站请求。

    citation cite_url 源自外部 AI provider 搜索结果(不可信),robots.txt 抓取会对
    该 host 直接发 HTTP 请求。若 host 解析到 loopback / RFC1918 私网 / 链路本地
    (含 169.254.169.254 云元数据) / 保留 / 组播 等非公网地址,一律判定不安全,
    禁止发起出站请求(fail-closed)。

    返回 True = 全部解析地址均为公网,可放行;False = 解析失败或命中内网地址。
    """
    if not host:
        return False
    try:
        addrs = _resolve_host_ips(host)
    except Exception:
        # 解析不了 → 保守拒绝(反正也爬不到,且防 DNS 异常绕过)
        return False
    if not addrs:
        return False
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        # 归一化 IPv4-mapped IPv6(如 ::ffff:127.0.0.1)再判定
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local  # 含 169.254.0.0/16 云元数据段
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


class _SSRFSafeRedirectHandler(HTTPRedirectHandler):
    """[GEO-R1-CAN-123] robots 抓取跟随重定向前,校验目标 host 非内网,防重定向绕过。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl).hostname
        if not _is_safe_public_host(target):
            raise HTTPError(newurl, code, "SSRF-blocked redirect to internal address", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 专用 opener:robots 抓取走它,重定向经 SSRF 校验
_SSRF_SAFE_OPENER = build_opener(_SSRFSafeRedirectHandler)


def _fetch_robots_parser(scheme: str, netloc: str, hostname: Optional[str]) -> Optional[RobotFileParser]:
    """真正发出站请求抓一个域的 robots.txt(无锁段, 由 _get_robots_parser_cached 串门)。

    语义与旧版逐条一致:
    - [GEO-R1-CAN-123] 发请求前再校验一次 host(纵深防御, 防被单独调用时的 SSRF);
      不安全 → None(上游已用 PRIVATE_ADDRESS 拦截该域全部 URL, 此处 None 只是占位)。
    - 抓取超时/任何异常 → None → 放行(不冒侵权险但也不阻塞跑批)。
    - 重定向经 _SSRF_SAFE_OPENER 逐跳校验。
    """
    if not _is_safe_public_host(hostname):
        return None
    rp = RobotFileParser()
    rp.set_url(f"{scheme}://{netloc}/robots.txt")
    try:
        start = time.monotonic()
        with _SSRF_SAFE_OPENER.open(rp.url, timeout=_ROBOTS_FETCH_TIMEOUT_SECONDS) as resp:
            # [出口审核 · 轮2修 · tarpit] read1() 单次 recv 语义(对 identity/定长
            # 响应 ≤ socket timeout 3s 返回已到达字节)+ 每块后查墙钟预算。必须 read1
            # 不能 read(n): read(n) 阻塞凑满 n 字节, 滴流下单块可拖数小时, 墙钟检查
            # 在块间永不触发。超预算 → None = 放行语义(硬要求 4)。
            # [轮3订正] chunked 响应下 read1 内部 readline 读 chunk-size 行会跨多次
            # recv, 单次 read1 仍可被逐字节滴流拖住 → 该类 worker 不由本墙钟自解放,
            # 由编排层总墙钟(guard-3)兜底(线程遗留 = 已知 LOW)。故不宣称本护栏
            # 覆盖全部正文滴流。
            chunks: List[bytes] = []
            remaining = 256 * 1024
            while remaining > 0:
                if time.monotonic() - start > _ROBOTS_TOTAL_READ_DEADLINE_SECONDS:
                    logger.warning(
                        f"[robots] {netloc} 读超总预算 "
                        f"{_ROBOTS_TOTAL_READ_DEADLINE_SECONDS:.0f}s, 放弃(按不可用放行)"
                    )
                    return None
                chunk = resp.read1(min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b''.join(chunks).decode('utf-8', errors='ignore')
        rp.parse(content.splitlines())
        return rp
    except Exception:
        return None


def _get_robots_parser_cached(
    scheme: str, netloc: str, hostname: Optional[str],
) -> Optional[RobotFileParser]:
    """线程安全取某域 robots parser(24h TTL 缓存 + 同域在飞去重)。

    并发契约(硬要求 6):
    - 缓存命中 → 直接返回(锁内读, 条目是一次性完整写入的, 无半成品);
    - 缓存未命中且无人在抓 → 本线程成为 fetcher, 锁外抓取, 完成后锁内写缓存
      + set Event(异常路径同样写 None + set, 防 waiter 悬挂/in-flight 泄漏);
    - 已有人在抓 → wait 同一个 Event(上限 = 抓取超时 + 裕量), 醒来后读缓存;
      极端竞态(wait 超时)兜底返回 None = 放行语义, 不重复发抓取。
    """
    now = time.time()
    with _ROBOTS_CACHE_LOCK:
        entry = _ROBOTS_CACHE.get(netloc)
        if entry and entry[1] > now:
            return entry[0]
        event = _ROBOTS_INFLIGHT.get(netloc)
        if event is None:
            event = threading.Event()
            _ROBOTS_INFLIGHT[netloc] = event
            i_am_fetcher = True
        else:
            i_am_fetcher = False

    if not i_am_fetcher:
        # [出口审核修] 等待上限对齐 fetcher 真实上界(connect 3s + 读预算 10s + 裕量),
        # 缩小"waiter 超时兜底放行 vs fetcher 真 disallow"的同轮分叉窗口
        # (分叉方向=放行·24h 缓存写入后自愈, 已知 LOW)。
        event.wait(
            timeout=_ROBOTS_FETCH_TIMEOUT_SECONDS + _ROBOTS_TOTAL_READ_DEADLINE_SECONDS + 5.0
        )
        with _ROBOTS_CACHE_LOCK:
            entry = _ROBOTS_CACHE.get(netloc)
            return entry[0] if entry and entry[1] > time.time() else None

    rp: Optional[RobotFileParser] = None
    try:
        rp = _fetch_robots_parser(scheme, netloc, hostname)
    except Exception:
        rp = None  # _fetch_robots_parser 已自兜, 此处纯防御
    finally:
        with _ROBOTS_CACHE_LOCK:
            _ROBOTS_CACHE[netloc] = (rp, time.time() + _ROBOTS_CACHE_TTL)
            _ROBOTS_INFLIGHT.pop(netloc, None)
        event.set()
    return rp


def _check_robots_txt(url: str) -> bool:
    """
    检查 URL 是否被该域名 robots.txt 禁止抓取。
    返回: True = 禁止(应过滤), False = 允许或 robots 不可用(放行)

    [2026-07-16] 内部改走线程安全缓存 _get_robots_parser_cached,
    单线程调用语义与旧版逐字一致(24h TTL / 不可用放行 / SSRF 纵深防御)。
    """
    parsed = urlparse(url)
    domain = parsed.netloc
    if not domain:
        return False

    rp = _get_robots_parser_cached(parsed.scheme, domain, parsed.hostname)
    if rp is None:
        return False  # 拿不到 robots,放行(不冒侵权险但也不阻塞跑批)

    return not rp.can_fetch(ROBOTS_UA, url)


def _shape_prefilter(url: str) -> Optional[PreFilterReason]:
    """纯形状过滤(零网络): 合法性 / 域名黑名单 / 文件后缀 / URL pattern。

    [2026-07-16] 从 should_pre_filter_url 逐字拆出, 供并发批量预筛先做
    廉价筛再合并网络检查; 单 URL 路径行为不变。
    """
    if not url or not isinstance(url, str):
        return PreFilterReason.URL_PATTERN

    parsed = urlparse(url.strip())
    if not parsed.netloc or parsed.scheme not in ('http', 'https'):
        return PreFilterReason.URL_PATTERN

    # 1. 域名黑名单
    if is_blacklisted(parsed.netloc):
        return PreFilterReason.BLACKLIST_DOMAIN

    # 2. 文件后缀
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in UNSUPPORTED_EXTENSIONS):
        return PreFilterReason.UNSUPPORTED_EXTENSION

    # 3. URL pattern
    if any(p in path_lower for p in URL_PATH_BLACKLIST_PATTERNS):
        return PreFilterReason.URL_PATTERN

    return None


def should_pre_filter_url(url: str, check_robots: bool = False) -> Optional[PreFilterReason]:
    """
    返回过滤原因, None 表示通过。

    check_robots=False 默认关闭 (robots 检查会发 HTTP 请求慢);
    backfill 单 URL 路径显式开启; 跑批 stage 2 已改走 prefilter_urls_concurrent
    (同语义的按域合并并发版)。
    """
    shape = _shape_prefilter(url)
    if shape is not None:
        return shape

    # 4. robots.txt (P0-A 版权防护, 可选)
    # 仅在会真正发出站请求的 check_robots 路径做 SSRF 校验(避免对纯形状过滤路径
    # 做无谓 DNS 解析)。
    if check_robots:
        parsed = urlparse(url.strip())
        # [GEO-R1-CAN-123] SSRF 防护:cite_url 不可信,解析到内网/回环/元数据地址一律拦截
        if not _is_safe_public_host(parsed.hostname):
            return PreFilterReason.PRIVATE_ADDRESS
        if _check_robots_txt(url):
            return PreFilterReason.ROBOTS_DISALLOWED

    return None


# ==================== [2026-07-16] Stage2 并发批量预筛 ====================

# 每个 origin(netloc)一个网络任务: host 安全校验(DNS) + robots parser 加载。
# 任务内部全自兜不抛; 未预期异常由收集侧按 fail-closed 处理。


def _origin_network_check(
    scheme: str,
    netloc: str,
    hostname: Optional[str],
    dns_verdicts: Dict[Optional[str], bool],
    dns_lock: threading.Lock,
    task_started_at: Optional[Dict[Tuple[str, str, Optional[str]], float]] = None,
) -> Tuple[bool, Optional[RobotFileParser]]:
    """单 origin 的网络检查: (host 是否安全公网, robots parser 或 None)。

    dns_verdicts: 本次跑批范围内的 hostname → 安全裁定缓存(同 hostname 跨
    scheme/端口只解析一次)。仅 run 级缓存, 不跨轮持久 — 与旧版"同一轮内同域
    多次 getaddrinfo 结果一致"的实际语义等价, 不引入跨轮 DNS 陈旧裁定;
    robots 抓取的重定向校验(_SSRFSafeRedirectHandler)仍逐跳新鲜解析, 不走本缓存。

    task_started_at: [出口审核修·tarpit 弃养] 开跑时间戳表(键=origin 三元组),
    供主 wait 循环判定超龄在飞任务。
    """
    if task_started_at is not None:
        with dns_lock:
            task_started_at[(scheme, netloc, hostname)] = time.monotonic()
    with dns_lock:
        cached = dns_verdicts.get(hostname)
    if cached is None:
        safe = _is_safe_public_host(hostname)  # fail-closed: 解析失败/内网 → False
        with dns_lock:
            dns_verdicts[hostname] = safe
    else:
        safe = cached
    if not safe:
        return (False, None)
    rp = _get_robots_parser_cached(scheme, netloc, hostname)
    return (True, rp)


def prefilter_urls_concurrent(
    urls: List[str],
    *,
    max_workers: int = 16,
    heartbeat_cb: Optional[Callable[[], None]] = None,
    should_stop_cb: Optional[Callable[[], bool]] = None,
    heartbeat_interval_seconds: float = 30.0,
) -> Tuple[Dict[str, Optional[PreFilterReason]], bool]:
    """按域合并的有界并发预筛(Stage2 专用 · 语义与逐 URL should_pre_filter_url
    (check_robots=True) 完全一致, 只是网络检查按 origin 合并 + 并发化)。

    返回 (verdicts, stopped):
      verdicts: {url: None=通过 / PreFilterReason=过滤原因}
      stopped:  True = should_stop_cb 判停(轮已取消), verdicts 不完整, 调用方
                应按取消处理; 已提交未开跑的任务被 cancel(不再发新网络请求),
                在飞任务不再等待(shutdown(wait=False)) — 正常任务上界 ≈
                connect 3s + 读预算 10s + DNS 后自然结束; 被 tarpit 钉死的
                线程会遗留至其自然结束(WARN 可见, 已知残余面)。

    并发/心跳契约:
    - 全局并发上限 = max_workers(单 ThreadPoolExecutor, 硬要求 1);
    - 主等待循环按 heartbeat_interval_seconds 切片 wait, 每片醒来即调
      heartbeat_cb(时间驱动, 不依赖完成条数, 硬要求 7); cb 异常吞掉只 log
      (DB/Redis 瞬断不得中断预筛, 也绝不在此标任何终态);
    - 每片醒来查 should_stop_cb(异常按未取消处理 + log), 判停即 cancel 未
      开跑任务并停止收集(硬要求 8);
    - SSRF 语义零弱化(硬要求 5): host 裁定 fail-closed(解析失败=拦);
      origin 任务未预期异常 → 该域全部 URL 记 PRIVATE_ADDRESS(fail-closed);
      重定向逐跳新鲜校验不经任何缓存。
    """
    verdicts: Dict[str, Optional[PreFilterReason]] = {}
    # Phase 1: 纯形状筛(零网络, 串行, 微秒级/条)
    by_origin: Dict[Tuple[str, str, Optional[str]], List[str]] = {}
    for url in urls:
        shape = _shape_prefilter(url)
        if shape is not None:
            verdicts[url] = shape
            continue
        parsed = urlparse(url.strip())
        key = (parsed.scheme, parsed.netloc, parsed.hostname)
        by_origin.setdefault(key, []).append(url)

    if not by_origin:
        return (verdicts, False)

    def _beat() -> None:
        if heartbeat_cb is None:
            return
        try:
            heartbeat_cb()
        except Exception as e:
            # DB/Redis 瞬断: 只 log 不中断(预筛完成与否由主流程决定, 此处零终态写入)
            logger.warning(f"[stage2-prefilter] heartbeat 回调失败(忽略继续): {e}")

    def _stopped() -> bool:
        if should_stop_cb is None:
            return False
        try:
            return bool(should_stop_cb())
        except Exception as e:
            logger.warning(f"[stage2-prefilter] 取消探测失败(按未取消继续): {e}")
            return False

    dns_verdicts: Dict[Optional[str], bool] = {}
    dns_lock = threading.Lock()
    task_started_at: Dict[Tuple[str, str, Optional[str]], float] = {}
    origin_results: Dict[Tuple[str, str, Optional[str]], Tuple[bool, Optional[RobotFileParser]]] = {}
    stopped = False
    abandoned_count = 0

    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix='stage2-prefilter',
    )
    try:
        future_to_key = {}
        # [出口审核修] 提交循环: 判停探测按时间节流(每次探测=一条 DB 查询,
        # 逐 origin 探测会对大轮打出 O(域数) 串行 DB 往返); 提交段同样按节拍
        # 穿插心跳(原先只有 wait 循环有, 提交段慢 DB 场景零心跳)。
        last_stop_probe = time.monotonic()
        last_submit_beat = time.monotonic()
        if _stopped():
            stopped = True
        for key in ([] if stopped else by_origin):
            now = time.monotonic()
            if now - last_stop_probe >= _STOP_PROBE_MIN_INTERVAL_SECONDS:
                last_stop_probe = now
                if _stopped():
                    stopped = True
                    break
            if now - last_submit_beat >= heartbeat_interval_seconds:
                _beat()
                last_submit_beat = now
            future_to_key[
                pool.submit(_origin_network_check, key[0], key[1], key[2],
                            dns_verdicts, dns_lock, task_started_at)
            ] = key

        # [出口审核 · 轮2修] 编排层总墙钟兜底: 头部滴流 worker 无法自解放(护栏1
        # 管不到 opener.open() 内的 header 读), 钉满 max_workers 后排队 origin 永无
        # started_at 永不被单任务弃养 → while pending 死循环 → 同步阻塞 event loop →
        # 连 run_round 4h wait_for 都触发不了 → 整轮无限期挂起。总墙钟保证无论何种
        # 滴流 prefilter 都有界返回。按 origin 数动态(正常大轮远低于, 仅 tarpit 触发)。
        total_deadline = min(
            _PREFILTER_MAX_TOTAL_DEADLINE_SECONDS,
            max(
                _PREFILTER_MIN_TOTAL_DEADLINE_SECONDS,
                ((len(by_origin) + max_workers - 1) // max_workers)
                * _PREFILTER_PER_ORIGIN_BUDGET_SECONDS,
            ),
        )
        orchestration_start = time.monotonic()
        timed_out_origins: set = set()  # 超时/弃养 → NETWORK_CHECK_TIMEOUT(非私网)

        pending = set(future_to_key)
        while pending and not stopped:
            done, pending = concurrent.futures.wait(
                pending, timeout=heartbeat_interval_seconds,
            )
            _beat()  # 时间驱动心跳: 即使本片零完成也刷新
            for fut in done:
                key = future_to_key[fut]
                try:
                    origin_results[key] = fut.result()
                except concurrent.futures.CancelledError:
                    pass
                except Exception as e:
                    # 未预期异常 → fail-closed: 该域按不安全处理
                    logger.warning(
                        f"[stage2-prefilter] origin 检查异常 fail-closed {key[1]}: {e}"
                    )
                    origin_results[key] = (False, None)
            # [出口审核修 · tarpit 单任务弃养] 已开跑但超龄(正常上界 ≈ connect 3s +
            # 读预算 10s + DNS)判为被滴流钉死: 该域 fail-closed 并放弃等待, 腾出
            # 账面额度。(非 chunked 正文滴流的 worker 会因护栏1的 read1+墙钟在
            # ≈13s 自行退出真腾额度; 头部滴流 / chunked 正文滴流 worker 无法自解放,
            # 靠下面总墙钟兜底。)
            if pending:
                now2 = time.monotonic()
                for fut in list(pending):
                    key = future_to_key[fut]
                    with dns_lock:
                        started = task_started_at.get(key)
                    if started is not None and now2 - started > _ORIGIN_CHECK_ABANDON_SECONDS:
                        origin_results[key] = (False, None)  # fail-closed
                        timed_out_origins.add(key)
                        pending.discard(fut)
                        fut.cancel()
                        abandoned_count += 1
                        logger.warning(
                            f"[stage2-prefilter] origin 检查超 "
                            f"{_ORIGIN_CHECK_ABANDON_SECONDS:.0f}s 弃养 fail-closed: {key[1]}"
                        )
            # 总墙钟兜底: 全 worker 被头部滴流钉死时排队 origin 永不弃养, 此处强制收尾
            if pending and time.monotonic() - orchestration_start > total_deadline:
                for fut in list(pending):
                    key = future_to_key[fut]
                    if key not in origin_results:
                        origin_results[key] = (False, None)  # fail-closed
                        timed_out_origins.add(key)
                    fut.cancel()
                logger.error(
                    f"[stage2-prefilter] 总墙钟预算 {total_deadline:.0f}s 耗尽, "
                    f"剩余 {len(pending)} origin fail-closed 收尾(疑似 tarpit 钉满 worker)"
                )
                pending = set()
                break
            if pending and _stopped():
                stopped = True
        if stopped:
            # 未开跑的任务撤下(不再发新网络请求); 在飞任务不等(可能被钉死),
            # 由 finally 的 shutdown(wait=False) 放它们自然收敛/遗留
            for fut in pending:
                fut.cancel()
    finally:
        # [出口审核修] 不等在飞线程: 正常完成时 pending 已空, wait=False 与
        # wait=True 等价(空闲线程即刻回收); 弃养/判停/总墙钟路径若 wait=True 会把
        # event loop 线程钉死在被 tarpit 占住的 worker 上(收敛上界不可证)。
        pool.shutdown(wait=False)

    if abandoned_count:
        logger.warning(
            f"[stage2-prefilter] 本轮弃养 {abandoned_count} 个超龄 origin 检查(全部 fail-closed)"
        )

    if stopped:
        return (verdicts, True)

    # Phase 3: 用 origin 结果给每个 URL 出裁定(纯 CPU)
    for key, url_list in by_origin.items():
        host_safe, rp = origin_results.get(key, (False, None))  # 缺结果 fail-closed
        timed_out = key in timed_out_origins
        for url in url_list:
            if timed_out:
                verdicts[url] = PreFilterReason.NETWORK_CHECK_TIMEOUT  # 慢站非私网
            elif not host_safe:
                verdicts[url] = PreFilterReason.PRIVATE_ADDRESS
            elif rp is not None and not rp.can_fetch(ROBOTS_UA, url):
                verdicts[url] = PreFilterReason.ROBOTS_DISALLOWED
            else:
                verdicts[url] = None

    return (verdicts, False)


async def _http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    max_retries: int = 2,
) -> tuple[httpx.Response, list[dict]]:
    """I2: 简单 retry · 网络错 / 5xx 重试 max_retries 次, 每次间隔 1s。"""
    last_exc: Optional[Exception] = None
    attempts: list[dict] = []
    for attempt in range(max_retries + 1):
        started = time.monotonic()
        try:
            resp = await client.get(url, headers=headers, timeout=60.0)
            resp.raise_for_status()
            attempts.append({
                "attempt_number": attempt + 1,
                "status": "success",
                "http_status": getattr(resp, "status_code", None),
                "latency_ms": round((time.monotonic() - started) * 1000),
            })
            return resp, attempts
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_exc = e
            response = getattr(e, "response", None)
            attempts.append({
                "attempt_number": attempt + 1,
                "status": "failed",
                "http_status": getattr(response, "status_code", None),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": f"{type(e).__name__}:{str(e)[:300]}",
            })
            if attempt < max_retries:
                await asyncio.sleep(1)
                continue
            raise
    raise last_exc  # type: ignore[misc]


async def crawl_article(url: str, jina_api_key: Optional[str] = None) -> Dict:
    """
    用 Jina Reader 爬取 URL 返回 markdown。

    返回:
    {
        'url': str,
        'title': str,
        'content': str (markdown 正文),
        'char_count': int,
        'fetched_at': str (ISO 时间戳),
        'ok': bool,
        'error': Optional[str],
    }
    """
    api_key = jina_api_key or os.getenv('JINA_API_KEY', '').strip()

    headers: Dict[str, str] = {
        'Accept': 'application/json',
        'X-Return-Format': 'markdown',
    }
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            limits=httpx.Limits(max_connections=10),
            proxy=JINA_HTTP_PROXY,  # P14.6: 仅 Jina 走代理 · None 时跟之前一样直连
        ) as client:
            # Retry ownership lives in round_runner so one budget counter equals
            # one real HTTP attempt.  Keeping retries in both layers previously
            # hid up to 3x requests and costs.
            resp, attempts = await _http_get_with_retry(
                client, JINA_BASE + url, headers, max_retries=0
            )
            payload = None
            try:
                parsed = resp.json()
                payload = parsed if isinstance(parsed, dict) else None
            except Exception:
                payload = None
            data = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                data = {}
            content = str(data.get('content') or data.get('markdown') or resp.text or '')

        # 提取 title (Jina 通常在 markdown 开头有 Title:)
        title = str(data.get('title') or '')
        first_line = content.split('\n', 1)[0] if content else ''
        if not title and first_line.startswith('Title:'):
            title = first_line[6:].strip()

        usage = data.get('usage') or (payload or {}).get('usage') or {}
        usage_tokens = usage.get('tokens') if isinstance(usage, dict) else None
        final_url = data.get('url') or data.get('finalUrl') or url
        published_time = data.get('publishedTime') or data.get('published_time')
        warning = data.get('warning') or (payload or {}).get('warning')

        return {
            'url': url,
            'title': title,
            'content': content,
            'char_count': len(content),
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'ok': True,
            'error': None,
            'final_url': final_url,
            'http_status': data.get('httpStatus') or getattr(resp, 'status_code', None),
            'warning': warning,
            'published_time': published_time,
            'usage_tokens': usage_tokens,
            'actual_attempts': len(attempts),
            'attempts': attempts,
            'request_profile': 'style_analysis_v1',
            'request_profile_version': 'jcc-v1.0',
            'parser_version': 'jina-json-markdown-v1',
            'raw_payload': payload,
        }
    except Exception as e:
        return {
            'url': url,
            'title': '',
            'content': '',
            'char_count': 0,
            'fetched_at': '',
            'ok': False,
            'error': str(e),
            'actual_attempts': len(locals().get('attempts') or []) or 1,
            'attempts': (locals().get('attempts') or [{
                'attempt_number': 1,
                'status': 'failed',
                'http_status': None,
                'latency_ms': None,
                'error': f"{type(e).__name__}:{str(e)[:300]}",
            }]),
            'request_profile': 'style_analysis_v1',
            'request_profile_version': 'jcc-v1.0',
            'parser_version': 'jina-json-markdown-v1',
        }
