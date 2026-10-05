# -*- coding: utf-8 -*-
"""WO_279 · 后端自己发的跳转不许把 https 访客带回 `http://` —— 两道闸,各自单独成立。

病灶(WO_279 实测:真 nginx 1.26.3 + 真 app;uvicorn 用 start.sh 同一条启动参数,版本与生产钉死的一致):
  尾斜杠不匹配(`GET /api/public/whitelabel/`)⇒ Starlette 307 `Location: http://omnirank.top/api/public/whitelabel`。
  nginx 把 X-Forwarded-Proto 写成 $scheme(容器只听明文 80,恒为 http),uvicorn 又信任本机 nginx 的代理头
  ⇒ 后端眼里的协议永远是 http,边缘传来的 `X-Forwarded-Proto: https` 也被覆盖掉。
  同一个「后端以为是 http」还让它拼的分享 / 报价 / 资料链接成了 http://,Cookie 也不带 Secure。

两道闸(任一单独被撤,都要有格变红):
  闸一 · 协议透传:每个 proxy_pass 到 backend 的 location 都 `proxy_set_header X-Forwarded-Proto $forwarded_proto`;
        http 级 map 把边缘原协议归一(http / https,不分大小写),缺了或不认识才退回 $scheme。
        ⇒ 边缘带 https 时,后端拼出来的 Location 就是 https://。
  闸二 · 同域 http 绝对 Location 改相对:server 级 `proxy_redirect http://$host/ /;` 在每个 backend location 实际生效。
        ⇒ 边缘不带 X-Forwarded-Proto 时(阿里云 ESA「托管转换」文档列的回源头里没有它),Location 也不会是 http://。

判据是静态 + 进程内的(不起 nginx / docker,任何跑 pytest 的地方都能跑):
  · nginx 语义用块树模型推演。继承规则:location 里只要写了任何一条 proxy_set_header(或 proxy_redirect),
    就不再继承上一级的同名指令 —— 所以「server 级写了」不等于「每个 location 生效」,要逐个 location 算。
  · uvicorn 的代理头处理用**真的** ProxyHeadersMiddleware,信任名单取 uvicorn 自己的默认值;
    Starlette 的尾斜杠 307 用**真的** FastAPI 路由(与生产同一版本);对端是本机回环(upstream 必须是回环地址)。
  · 生产启动行(start.sh 里 `uvicorn server:app` 那一行)不许关代理头、不许改信任名单 —— 否则闸一在生产上不生效。
  模型与真 nginx + 真 app 逐条对表的读数在交付单里(同一份 conf 改前 / 改后)。

牙证 / 对照:
  · 反臂(真 conf 上撤):撤闸一 ⇒ 边缘带 https 也只拿到相对地址;撤闸二 ⇒ 边缘不带时拿到 http://;
    两道都撤 ⇒ 推演结果正是改前的真读数 `http://omnirank.top/api/public/whitelabel`(边缘带不带 https 都一样)。
  · 合成 conf:location 写了 Host 却没写 X-Forwarded-Proto(继承陷阱)⇒ 红;location 里 `proxy_redirect off` ⇒ 红;
    map 直接透传原值 ⇒ 空值 / 乱值推演不对 ⇒ 红。
    对照:location 一条 proxy_set_header 都不写 ⇒ 继承 server 级 ⇒ 绿;https:// 与外域 Location 不被改写。
  · [WO_279b] 指令原文只许出现在指令行上:注释里抄了原文,按字符串下的毒 / grep 会先落在注释上。
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONF = ROOT / "nginx.conf"
START_SH = ROOT / "start.sh"
HOST = "omnirank.top"
PROBE = "/api/public/whitelabel"  # 真实存在的公开路由(api/share_api.py),路由本身不带尾斜杠
BACKEND = "http://backend"
MAPPED = "$forwarded_proto"
BEFORE = f"http://{HOST}{PROBE}"  # 改前真读数(边缘带不带 https 都是它)
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


# ---------------------------------------------------------------------------
# 解析(分词与块树沿用 WO_278 的 tests/nginx_relative_redirects_2026_09_23)
# ---------------------------------------------------------------------------

@dataclass
class Directive:
    name: str
    args: list
    block: list | None = None


def _tokenize(text: str) -> list:
    toks, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in "{};":
            toks.append(c)
            i += 1
        elif c in "\"'":
            j, buf = i + 1, []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            toks.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "{};":
                j += 1
            toks.append(text[i:j])
            i = j
    return toks


def parse(text: str) -> list:
    toks, pos = _tokenize(text), 0

    def block() -> list:
        nonlocal pos
        items = []
        while pos < len(toks):
            if toks[pos] == "}":
                pos += 1
                return items
            words = []
            while pos < len(toks) and toks[pos] not in ("{", ";", "}"):
                words.append(toks[pos])
                pos += 1
            if pos >= len(toks):
                raise ValueError("配置在指令中间结束了")
            if toks[pos] == ";":
                pos += 1
                items.append(Directive(words[0], words[1:]))
            elif toks[pos] == "{":
                pos += 1
                items.append(Directive(words[0], words[1:], block()))
            else:
                raise ValueError(f"孤立的 }} 前面是 {words}")
        return items

    tree = block()
    if pos != len(toks):
        raise ValueError("多余的 }")
    return tree


@dataclass
class Location:
    mod: str
    pattern: str
    body: list

    @property
    def name(self) -> str:
        return f"{self.mod} {self.pattern}".strip()


# [WO_293] 容器 nginx 两个 server 块:应用兜底块 `_` + 官网 omnirank.cn 块。
#   多了 / 少了 / 改了名都要先改判据。转给 backend 的 location 在哪个块里都要守闸一闸二。
SERVER_NAMES = (("_",),)


def all_servers(tree: list) -> list:
    return [d for d in tree if d.name == "server" and d.block is not None]


def server_names(tree: list) -> list:
    return sorted(tuple(x.args) for s in all_servers(tree) for x in s.block if x.name == "server_name")


def the_server(tree: list) -> Directive:
    """应用兜底块(server_name _):尾斜杠 307 那条链走的是它。"""
    assert server_names(tree) == sorted(SERVER_NAMES), \
        f"期望 server 块恰为 {SERVER_NAMES},实际 {server_names(tree)}(判据按这几块写,变了要先改判据)"
    return next(s for s in all_servers(tree)
                if any(x.name == "server_name" and x.args == ["_"] for x in s.block))


def locations(server: Directive) -> list:
    out = []
    for d in server.block:
        if d.name == "location":
            mod, pat = (d.args if len(d.args) == 2 else ("", d.args[0]))
            out.append(Location(mod, pat, d.block or []))
    return out


# [Review 09-28 ③] 门户块 /v1/ 反代带 URI(http://backend/api/public/open/v1/)—— 同样是转给后端,同样要守闸一闸二
def _to_backend(target: str) -> bool:
    return target == BACKEND or target.startswith(BACKEND + "/")


_PASS_RX = re.compile(r"proxy_pass http://backend[;/]")


def backend_locations(server: Directive) -> list:
    return [l for l in locations(server)
            if any(d.name == "proxy_pass" and d.args[:1] and _to_backend(d.args[0]) for d in l.body)]


def select(locs: list, uri: str) -> Location | None:
    """nginx 选 location:`=` 精确 > 最长前缀若带 `^~` 即取 > 正则按文件序首个命中 > 最长前缀。"""
    for loc in locs:
        if loc.mod == "=" and loc.pattern == uri:
            return loc
    prefixes = [l for l in locs if l.mod in ("", "^~") and uri.startswith(l.pattern)]
    best = max(prefixes, key=lambda l: len(l.pattern), default=None)
    if best is not None and best.mod == "^~":
        return best
    for loc in locs:
        if loc.mod in ("~", "~*") and re.search(loc.pattern, uri, re.I if loc.mod == "~*" else 0):
            return loc
    return best


# ---------------------------------------------------------------------------
# nginx 语义模型:继承 / map / proxy_redirect
# ---------------------------------------------------------------------------

def effective(tree: list, server: Directive, loc: Location, name: str) -> list:
    """proxy_set_header / proxy_redirect 的继承:本级写了任何一条就只用本级,否则取上一级(location → server → http)。"""
    for level in (loc.body, server.block, tree):
        own = [d for d in level if d.name == name]
        if own:
            return own
    return []


def headers_sent(tree, server, loc) -> dict:
    out = {}
    for d in effective(tree, server, loc, "proxy_set_header"):
        key = d.args[0].lower()
        assert key not in out, f"location {loc.name} 重复设置了 {d.args[0]}"
        out[key] = d.args[1] if len(d.args) > 1 else ""
    return out


@dataclass
class Map:
    src: str
    default: str
    exact: dict
    regex: list


def maps(tree: list) -> dict:
    out = {}
    for d in tree:
        if d.name != "map" or d.block is None or len(d.args) != 2:
            continue
        src, dst = d.args
        default, exact, regex = "", {}, []
        for e in d.block:
            val = e.args[0] if e.args else ""
            if e.name == "default":
                default = val
            elif e.name.startswith("~*"):
                regex.append((re.I, e.name[2:], val))
            elif e.name.startswith("~"):
                regex.append((0, e.name[1:], val))
            else:
                exact[e.name] = val
        out[dst] = Map(src, default, exact, regex)
    return out


def _expand(value: str, edge: str) -> str:
    # 容器只听明文 80 ⇒ $scheme 恒为 http;其它变量本判据不认,原样留着让断言报出来
    return {"$scheme": "http", "$http_x_forwarded_proto": edge}.get(value, value)


def eval_map(m: Map, edge: str) -> str:
    """nginx map 取值顺序:精确键 > 正则键按出现顺序 > default。"""
    if m.src != "$http_x_forwarded_proto":
        return f"<map 源不是 $http_x_forwarded_proto 而是 {m.src}>"
    if edge in m.exact:
        return _expand(m.exact[edge], edge)
    for flags, pat, val in m.regex:
        if re.search(pat, edge, flags):
            return _expand(val, edge)
    return _expand(m.default, edge)


def xfp_to_backend(tree, server, loc, edge: str | None) -> str | None:
    """nginx 发给后端的 X-Forwarded-Proto;不发 ⇒ None(值算出来是空串时 nginx 也不发这个头)。"""
    v = headers_sent(tree, server, loc).get("x-forwarded-proto")
    if v is None:
        return None
    ms = maps(tree)
    got = eval_map(ms[v], edge or "") if v in ms else _expand(v, edge or "")
    return got or None


def rewrite_location(tree, server, loc, value: str, host: str = HOST) -> str:
    """nginx proxy_redirect:按生效的规则逐条试,首条命中即改;没写任何规则 ⇒ 隐式 default。"""
    rules = effective(tree, server, loc, "proxy_redirect") or [Directive("proxy_redirect", ["default"])]
    for d in rules:
        if d.args == ["off"]:
            return value
        if d.args == ["default"]:
            pp = next(x.args[0] for x in loc.body if x.name == "proxy_pass")
            pat, rep = pp.rstrip("/") + "/", "/"  # proxy_pass 不带 URI 时 nginx 的默认规则
        elif len(d.args) == 2:
            pat, rep = d.args
        else:
            continue
        if pat.startswith("~"):
            rx = pat[2:] if pat.startswith("~*") else pat[1:]
            m = re.match(rx, value, re.I if pat.startswith("~*") else 0)
            if m:
                return re.sub(r"\$(\d)", lambda g: m.group(int(g.group(1))) or "", rep)
            continue
        pat = pat.replace("$host", host).replace("$proxy_host", BACKEND[len("http://"):])
        if value.startswith(pat):
            return rep + value[len(pat):]
    return value


def upstream_hosts(tree: list) -> list:
    ups = [d for d in tree if d.name == "upstream" and d.args == [BACKEND[len("http://"):]] and d.block]
    assert len(ups) == 1, "找不到 upstream backend"
    return [d.args[0].rsplit(":", 1)[0] for d in ups[0].block if d.name == "server"]


# ---------------------------------------------------------------------------
# 进程内的真 uvicorn 代理头处理 + 真 Starlette 尾斜杠 307
# ---------------------------------------------------------------------------

def backend_307(xfp: str | None, peer: str) -> tuple:
    import os

    from fastapi import FastAPI
    from uvicorn.config import Config
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    saved = os.environ.pop("FORWARDED_ALLOW_IPS", None)  # 取 uvicorn 自己的默认信任名单(生产没设这个变量)
    try:
        cfg = Config(app="server:app")
        assert cfg.proxy_headers is True, "uvicorn 默认不再处理代理头了 —— 判据前提变了"
        trusted = cfg.forwarded_allow_ips
    finally:
        if saved is not None:
            os.environ["FORWARDED_ALLOW_IPS"] = saved

    app = FastAPI()

    @app.get(PROBE)
    def _probe():
        return {}

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    headers = [(b"host", HOST.encode())]
    if xfp is not None:
        headers.append((b"x-forwarded-proto", xfp.encode()))
    path = PROBE + "/"
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.0", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": b"", "headers": headers, "client": (peer, 50000), "server": ("127.0.0.1", 8000)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(wrapped(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], hdrs.get("location")


def chain(text: str, edge: str | None) -> str:
    """边缘 → nginx(选 location、算 X-Forwarded-Proto)→ uvicorn + Starlette 307 → nginx proxy_redirect → 浏览器拿到的 Location。"""
    tree = parse(text)
    srv = the_server(tree)
    loc = select(locations(srv), PROBE + "/")
    assert loc is not None and loc in backend_locations(srv), f"{PROBE}/ 没落到转给 backend 的 location"
    hosts = upstream_hosts(tree)
    peer = "127.0.0.1" if hosts and all(h in _LOOPBACK for h in hosts) else hosts[0]
    status, location = backend_307(xfp_to_backend(tree, srv, loc, edge), peer)
    assert status == 307 and location, f"后端没有发尾斜杠 307(status={status})—— 判据前提变了"
    return rewrite_location(tree, srv, loc, location)


# ---------------------------------------------------------------------------
# 违规清单(真 conf 应为空)
# ---------------------------------------------------------------------------

def forwarding_problems(text: str) -> list:
    tree = parse(text)
    out = []
    for srv, loc in ((s, l) for s in all_servers(tree) for l in backend_locations(s)):
        h = headers_sent(tree, srv, loc)
        if h.get("x-forwarded-proto") != MAPPED:
            out.append(f"location {loc.name}:X-Forwarded-Proto = {h.get('x-forwarded-proto')!r},要 {MAPPED}")
        if h.get("host") != "$host":
            out.append(f"location {loc.name}:Host = {h.get('host')!r},要 $host")
    return out


def redirect_problems(text: str) -> list:
    tree = parse(text)
    out = []
    for srv, loc in ((s, l) for s in all_servers(tree) for l in backend_locations(s)):
        rel = rewrite_location(tree, srv, loc, f"http://{HOST}{PROBE}")
        if rel != PROBE:
            out.append(f"location {loc.name}:后端的 http://{HOST}/… 没被改成相对路径(得到 {rel})")
        for keep in (f"https://{HOST}{PROBE}", "http://pay.example.com/notify"):
            if rewrite_location(tree, srv, loc, keep) != keep:
                out.append(f"location {loc.name}:误改了不该改的 {keep}")
    return out


_MAP_CASES = {"https": "https", "HTTPS": "https", "http": "http", "": "http",
              "https, http": "http", "javascript": "http", "wss": "http"}


def map_problems(text: str) -> list:
    ms = maps(parse(text))
    if MAPPED not in ms:
        return [f"没有 `map $http_x_forwarded_proto {MAPPED}`"]
    got = {edge: eval_map(ms[MAPPED], edge) for edge in _MAP_CASES}
    return [f"边缘 X-Forwarded-Proto={e!r} ⇒ {got[e]!r},要 {want!r}" for e, want in _MAP_CASES.items() if got[e] != want]


def uvicorn_line_problems(text: str) -> list:
    lines = [l for l in text.splitlines()
             if "uvicorn" in l and "server:app" in l and not l.lstrip().startswith(("#", "echo"))]
    if len(lines) != 1:
        return [f"start.sh 里 `uvicorn server:app` 启动行 {len(lines)} 条(期望 1)"]
    out = []
    for bad in ("--no-proxy-headers", "--forwarded-allow-ips", "FORWARDED_ALLOW_IPS"):
        if bad in lines[0]:
            out.append(f"启动行带了 {bad} —— uvicorn 可能不再信任本机 nginx 的 X-Forwarded-Proto")
    return out


# ---------------------------------------------------------------------------
# 格
# ---------------------------------------------------------------------------

def _real() -> str:
    return CONF.read_text(encoding="utf-8")


def test_parser_sees_the_real_config():
    text = _real()
    srvs = all_servers(parse(text))
    assert len(srvs) == len(server_names(parse(text))) == len(SERVER_NAMES)
    assert len(locations(the_server(parse(text)))) >= 40
    assert sum(len(locations(s)) for s in srvs) == len(re.findall(r"(?m)^\s*location\s", text))
    assert sum(len(backend_locations(s)) for s in srvs) == len(_PASS_RX.findall(text)) >= 20
    assert len(maps(parse(text))) == len(re.findall(r"(?m)^map\s", text)) == 1


def test_every_backend_location_forwards_the_mapped_proto():
    assert forwarding_problems(_real()) == []


def test_map_normalizes_the_edge_proto():
    assert map_problems(_real()) == []


def test_backend_http_location_is_rewritten_to_relative_everywhere():
    assert redirect_problems(_real()) == []


def test_production_uvicorn_trusts_the_loopback_nginx():
    assert uvicorn_line_problems(START_SH.read_text(encoding="utf-8")) == []
    hosts = upstream_hosts(parse(_real()))
    assert hosts and all(h in _LOOPBACK for h in hosts), \
        f"upstream backend 不是本机回环 {hosts} —— uvicorn 默认只信任 127.0.0.1 的代理头,闸一在生产不生效"


_LIT = f"proxy_set_header X-Forwarded-Proto {MAPPED}"


def directive_text_outside_directive_lines(text: str) -> list:
    """[WO_279b] 指令原文出现在「不是这条指令本身」的行上(注释里、别的指令的字符串里)⇒ 列出来。"""
    return [f"第 {n} 行:{line.strip()[:70]}" for n, line in enumerate(text.splitlines(), 1)
            if _LIT in line and not line.lstrip().startswith(_LIT)]


def test_the_locked_directive_text_only_appears_on_directive_lines():
    """[WO_279b] 注释里抄了指令原文,按字符串找「第一处」的毒 / grep / 计数就会先落在注释上。
    WO_279 时 Review 的毒③(`sed 0,/串/`)打中的正是 map 上方的注释,锁全绿,被读成「锁不管 28 处齐不齐」;
    换打真指令行立刻 2 红。这里不数原文处数:数处数不认继承,合法重构会误红。"""
    text = _real()
    assert _LIT in text
    assert directive_text_outside_directive_lines(text) == []
    assert directive_text_outside_directive_lines(f"# 每个 location 都写 `{_LIT};`\n")        # 牙:注释里抄原文
    assert directive_text_outside_directive_lines(f'    add_header X-Note "{_LIT}";\n')       # 牙:别的指令的字符串里
    assert directive_text_outside_directive_lines(f"        {_LIT};  # 行尾注释\n") == []    # 对照:指令行带行尾注释


def test_chain_edge_https_gives_https_location():
    assert chain(_real(), "https") == f"https://{HOST}{PROBE}"


def test_chain_edge_silent_gives_relative_location():
    got = chain(_real(), None)
    assert got == PROBE and "http://" not in got


def test_arms_on_the_real_config():
    text = _real()
    # 只认缩进开头的指令行:map 上方的注释里也原样写着这句指令,按字符串数会多数一条
    xfp_rx = re.compile(r"(?m)^(\s+)proxy_set_header X-Forwarded-Proto \$forwarded_proto;")
    redir_line = "    proxy_redirect http://$host/ /;\n"
    n_xfp = len(xfp_rx.findall(text))
    assert n_xfp == len(_PASS_RX.findall(text)) and text.count(redir_line) == len(SERVER_NAMES), "反臂没下成"

    no_gate1 = xfp_rx.sub(r"\1proxy_set_header X-Forwarded-Proto $scheme;", text)
    assert len(forwarding_problems(no_gate1)) == n_xfp
    assert chain(no_gate1, "https") == PROBE  # 协议没透传:边缘带 https 也只拿到相对地址

    no_gate2 = text.replace(redir_line, "")
    assert len(redirect_problems(no_gate2)) == n_xfp
    assert chain(no_gate2, None) == BEFORE

    neither = no_gate1.replace(redir_line, "")
    assert chain(neither, None) == BEFORE and chain(neither, "https") == BEFORE  # = 改前两条真读数

    # [WO_293] 只在官网块里撤:应用块原样 ⇒ 仍必须各红一条(判据不许只看应用块)
    site_at = text.index("server_name omnirank.cn")
    head, site = text[:site_at], text[site_at:site_at + text[site_at:].index("\n}\n")]
    tail = text[site_at + len(site):]
    assert xfp_rx.search(site) and redir_line in site, "反臂没下成(官网块里找不到两道闸)"
    only_site_gate1 = head + xfp_rx.sub(r"\1proxy_set_header X-Forwarded-Proto $scheme;", site) + tail
    assert [p.split(":")[0] for p in forwarding_problems(only_site_gate1)] == ["location = /api/public/feature-costs"]
    only_site_gate2 = head + site.replace(redir_line, "") + tail
    assert [p.split(":")[0] for p in redirect_problems(only_site_gate2)] == ["location = /api/public/feature-costs"]


_SYNTH = """
map $http_x_forwarded_proto $forwarded_proto { default $scheme; ~*^https$ https; ~*^http$ http; }
upstream backend { server 127.0.0.1:8000; }
server {
    listen 80;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $forwarded_proto;
    proxy_redirect http://$host/ /;
    location /inherit/ { proxy_pass http://backend; }
    location /trap/ { proxy_pass http://backend; proxy_set_header Host $host; }
    location /off/ { proxy_pass http://backend; proxy_set_header Host $host;
                     proxy_set_header X-Forwarded-Proto $forwarded_proto; proxy_redirect off; }
    location /ok/ { proxy_pass http://backend; proxy_set_header Host $host;
                    proxy_set_header X-Forwarded-Proto $forwarded_proto; }
    location /static/ { root /srv; }
}
"""


def test_synthetic_teeth_and_controls():
    fwd = forwarding_problems(_SYNTH)
    assert len(fwd) == 1 and fwd[0].startswith("location /trap/:X-Forwarded-Proto"), fwd  # 继承陷阱
    red = redirect_problems(_SYNTH)
    assert len(red) == 1 and red[0].startswith("location /off/:"), red  # proxy_redirect off
    assert map_problems(_SYNTH) == []  # 对照:与真 conf 同形的 map

    raw_passthrough = _SYNTH.replace("default $scheme; ~*^https$ https; ~*^http$ http;",
                                     "default $http_x_forwarded_proto;")
    probs = map_problems(raw_passthrough)
    assert any("''" in p for p in probs) and any("javascript" in p for p in probs), probs

    for flag in ("--no-proxy-headers", "--forwarded-allow-ips 10.0.0.1"):
        line = f"python -m uvicorn server:app --host 0.0.0.0 --port 8000 {flag} &"
        assert uvicorn_line_problems(line), flag
    assert uvicorn_line_problems("python -m uvicorn server:app --host 0.0.0.0 --port 8000 &") == []
