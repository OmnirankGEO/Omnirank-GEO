# -*- coding: utf-8 -*-
"""WO_278 · 容器 nginx 的跳转必须发相对 Location —— 不许把 https 访客推回 `http://`。

病灶(Deploy 09-23 实测):
  `/social` → 302 `http://omnirank.top/s`;`/api` → 301 `http://omnirank.top/api/`。
  容器只听明文 80,不知道前面是 TLS;nginx 默认 `absolute_redirect on`,把相对目标拼成 `http://<Host>/…`。

判据是**静态**的(不起服务):这个包要能在任何跑 pytest 的地方跑,不能依赖 docker / nginx 二进制。
  ① 把 nginx.conf 解析成块树(去注释、认引号)。server 级必须有 `absolute_redirect off` 与
     `port_in_redirect off`,任何 location 不许把它们改回 on。
  ② 按 nginx 选 location 的规则(`=` 精确 > 最长前缀若带 `^~` 即取 > 正则按文件序首个命中 > 最长前缀),
     加上三种跳转来源:
       · `return 30x`;
       · `rewrite … redirect|permanent`;
       · 「以 / 结尾的前缀 location + *_pass」对去掉尾斜杠请求的自动 301 —— 这一种不再看正则。
     推演 `/social`、`/social/x`、`/api` 三条请求**实际发出**的 Location,断言都是相对路径、不含 `http://`。
  ③ 全文件任何 return / rewrite 的跳转目标不许写死 `http://` 或 `$scheme://`(容器里 $scheme 恒为 http);
     必须写绝对地址时,scheme 用 `$http_x_forwarded_proto`。
  模型对不对得上真 nginx:交付单附同一份 conf 在真 nginx(与生产同一个 Debian nginx 包)里改前 / 改后的读数。

牙证 / 对照:
  · 反臂(工单点名):从真 nginx.conf 里删掉那两条指令 ⇒ 三条 Location 全变 `http://…` ⇒ 红。
  · 合成 conf:写死 `http://` 目标 ⇒ ③ 红;location 里 `absolute_redirect on` ⇒ ① ② 红。
  · 对照:没动过的 nginx.conf 全绿;不产生跳转的路径推演为「无跳转」;https 绝对目标与
    `$http_x_forwarded_proto` 目标不误伤。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONF = ROOT / "nginx.conf"
PROBES = {  # 工单点名的三条 ⇒ (期望状态码, 期望目标)
    "/social": (302, "/s"),
    "/social/x": (302, "/s/x"),
    "/api": (301, "/api/"),
}
HOST = "omnirank.top"
_PASS = {"proxy_pass", "fastcgi_pass", "uwsgi_pass", "scgi_pass", "memcached_pass", "grpc_pass"}
_REDIRECT_CODES = {301, 302, 303, 307, 308}


# ---------------------------------------------------------------------------
# 解析
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


def servers(tree: list) -> list:
    return [d for d in tree if d.name == "server" and d.block is not None]


# [WO_293] 容器 nginx 两个 server 块:应用兜底块 `_` + 官网 omnirank.cn 块。
#   两条指令与「不写死明文 scheme」每个块都要守;工单点名的三条探针是应用块的路由,只对应用块推演。
SERVER_NAMES = (("_",),)


def _names(srv: Directive) -> list:
    return [tuple(d.args) for d in srv.block if d.name == "server_name"]


def is_app_server(srv: Directive) -> bool:
    """应用兜底块;合成 conf 不写 server_name 时也当应用块。"""
    return _names(srv) in ([], [("_",)])


def app_server(tree: list) -> Directive:
    apps = [s for s in servers(tree) if is_app_server(s)]
    assert len(apps) == 1, f"应用兜底块应恰 1 个,实得 {len(apps)}"
    return apps[0]


def _flag(block: list, name: str) -> str | None:
    vals = [d.args[0] for d in block if d.name == name and d.args]
    return vals[-1] if vals else None


@dataclass
class Location:
    mod: str
    pattern: str
    body: list


def locations(server: Directive) -> list:
    out = []
    for d in server.block:
        if d.name != "location":
            continue
        if len(d.args) == 2:
            mod, pat = d.args
        else:
            mod, pat = "", d.args[0]
        out.append(Location(mod, pat, d.block or []))
    return out


# ---------------------------------------------------------------------------
# 推演
# ---------------------------------------------------------------------------

def select(locs: list, uri: str) -> tuple:
    """返回 (location, 是否自动加斜杠 301)。"""
    for loc in locs:
        if loc.mod == "=" and loc.pattern == uri:
            return loc, False
    for loc in locs:  # 自动 301:前缀以 / 结尾 + *_pass,请求恰是它去掉尾斜杠 —— 不再看正则
        if loc.mod in ("", "^~") and loc.pattern == uri + "/" and \
                any(d.name in _PASS for d in loc.body):
            return loc, True
    prefixes = [l for l in locs if l.mod in ("", "^~") and uri.startswith(l.pattern)]
    best = max(prefixes, key=lambda l: len(l.pattern), default=None)
    if best is not None and best.mod == "^~":
        return best, False
    for loc in locs:
        if loc.mod == "~" and re.search(loc.pattern, uri):
            return loc, False
        if loc.mod == "~*" and re.search(loc.pattern, uri, re.I):
            return loc, False
    return best, False


def _is_absolute(target: str) -> bool:
    return bool(re.match(r"(?i)^(https?://|\$scheme://|\$http_x_forwarded_proto://)", target))


def _redirect_in(body: list, uri: str) -> tuple | None:
    """location 体(或 server 级)里 return / rewrite 产生的跳转 ⇒ (码, 目标);没有 ⇒ None。"""
    for d in body:
        if d.name == "return" and d.args:
            if len(d.args) == 1 and _is_absolute(d.args[0]):
                return 302, d.args[0]
            if d.args[0].isdigit() and int(d.args[0]) in _REDIRECT_CODES and len(d.args) > 1:
                return int(d.args[0]), d.args[1]
            return None  # 其它 return(410 / 404 / 文本)不是跳转,且到此为止
        if d.name == "rewrite" and len(d.args) >= 2:
            m = re.search(d.args[0], uri)
            if not m:
                continue
            target = re.sub(r"\$(\d)", lambda g: m.group(int(g.group(1))) or "", d.args[1])
            flag = d.args[2] if len(d.args) > 2 else ""
            if flag in ("redirect", "permanent") or _is_absolute(d.args[1]):
                return (301 if flag == "permanent" else 302), target
    return None


def emitted(server: Directive, uri: str) -> tuple | None:
    """一条请求实际得到的 (码, Location);不跳转 ⇒ None。"""
    server_level = _redirect_in([d for d in server.block if d.name in ("return", "rewrite")], uri)
    loc, auto = (None, False) if server_level else select(locations(server), uri)
    if server_level:
        code, target, loc_body = server_level[0], server_level[1], []
    elif auto:
        code, target, loc_body = 301, uri + "/", loc.body
    elif loc is not None and (r := _redirect_in(loc.body, uri)):
        code, target, loc_body = r[0], r[1], loc.body
    else:
        return None
    if _is_absolute(target):
        return code, target
    # nginx 继承:location 里写了就用 location 的,没写才继承 server 的,都没写默认 on
    setting = _flag(loc_body, "absolute_redirect") or _flag(server.block, "absolute_redirect") or "on"
    return code, (f"http://{HOST}{target}" if setting == "on" else target)


def violations(text: str, probes=PROBES) -> list:
    out = []
    for srv in servers(parse(text)):
        for name in ("absolute_redirect", "port_in_redirect"):
            if _flag(srv.block, name) != "off":
                out.append(f"server 级缺 `{name} off`")
        for loc in locations(srv):
            for name in ("absolute_redirect", "port_in_redirect"):
                if _flag(loc.body, name) == "on":
                    out.append(f"location {loc.mod} {loc.pattern} 把 `{name}` 改回 on")
        for uri, (code, target) in (probes.items() if is_app_server(srv) else ()):
            got = emitted(srv, uri)
            if got is None:
                out.append(f"{uri} 推演为不跳转(期望 {code} → {target})—— 模型或配置变了,先核")
                continue
            if not got[1].startswith("/") or "http://" in got[1]:
                out.append(f"{uri} 发出的 Location = {got[1]}(不是相对路径)")
        for d in _all_redirect_directives(srv.block):
            tgt = d.args[1] if d.name == "rewrite" else (d.args[-1] if d.args else "")
            if re.match(r"(?i)^(http://|\$scheme://)", tgt):
                out.append(f"`{d.name} {' '.join(d.args)}` 写死了明文 scheme")
    return out


def _all_redirect_directives(block: list):
    for d in block:
        if d.name in ("return", "rewrite"):
            yield d
        if d.block:
            yield from _all_redirect_directives(d.block)


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------

def _real() -> str:
    return CONF.read_text(encoding="utf-8")


def test_parser_sees_the_real_config():
    """分母自证:解析器真的读懂了这份 conf,不是零分母的绿。"""
    text = _real()
    tree = parse(text)
    srvs = servers(tree)
    assert sorted(n for s in srvs for n in _names(s)) == sorted(SERVER_NAMES) and len(srvs) == len(SERVER_NAMES), \
        f"期望 server 块恰为 {SERVER_NAMES},实得 {[_names(s) for s in srvs]}"
    locs = [l for s in srvs for l in locations(s)]
    # 与一把**独立**的尺子对账(按行数 `location` 开头的非注释行),不写死一个数
    independent = len(re.findall(r"(?m)^\s*location\s", text))
    assert len(locs) == independent, (len(locs), independent)
    assert len(locations(app_server(tree))) >= 40
    assert any(d.name == "upstream" for d in tree), "upstream backend 没解析出来"
    for loc in locs:  # 模型只覆盖一层 location;有人加嵌套 location 就得先扩模型
        assert not any(d.name == "location" for d in loc.body), f"{loc.pattern} 里有嵌套 location"


def test_the_three_probes_are_redirects_and_relative():
    srv = app_server(parse(_real()))
    for uri, (code, target) in PROBES.items():
        got = emitted(srv, uri)
        assert got == (code, target), f"{uri}: 推演得 {got},期望 ({code}, {target!r})"


def test_real_config_has_no_violation():
    assert violations(_real()) == []


def test_removing_the_directives_turns_every_probe_absolute():
    """反臂(工单点名):删掉那两条指令 ⇒ 三条 Location 全变 http:// ⇒ 红。"""
    text = _real()
    stripped = re.sub(r"(?m)^\s*(absolute_redirect|port_in_redirect)\s+off;\s*$", "", text)
    assert stripped != text, "施毒自证:没删到指令"
    srv = app_server(parse(stripped))
    for uri, (code, target) in PROBES.items():
        assert emitted(srv, uri) == (code, f"http://{HOST}{target}"), uri
    v = violations(stripped)
    assert sum("不是相对路径" in x for x in v) == 3, v
    assert sum("server 级缺" in x for x in v) == 2 * len(SERVER_NAMES), v
    # [WO_293] 只在官网块里删:应用块原样 ⇒ 仍红两条(判据不许只看应用块)
    site_at = text.index("server_name omnirank.cn")
    site_end = site_at + text[site_at:].index("\n}\n")
    only_site = text[:site_at] + re.sub(r"(?m)^\s*(absolute_redirect|port_in_redirect)\s+off;\s*$", "",
                                        text[site_at:site_end]) + text[site_end:]
    assert only_site != text, "施毒自证:官网块里没删到指令"
    assert violations(only_site) == ["server 级缺 `absolute_redirect off`", "server 级缺 `port_in_redirect off`"]


_SYNTH = """
upstream backend { server 127.0.0.1:8000; }
server {
    listen 80;
    absolute_redirect off;
    port_in_redirect off;
    location = /social { return 302 /s; }
    location ^~ /social/ { rewrite ^/social(/.*)$ /s$1 redirect; }
    location /api/ { proxy_pass http://backend; }
    location /assets/ { root /app; }
    location = /sw.js { return 410; }
    location = /cdn { return 301 https://cdn.example.com/x; }
    location = /fwd { return 302 $http_x_forwarded_proto://$host/y; }
    %EXTRA%
}
"""


def test_synthetic_teeth_and_controls():
    base = _SYNTH.replace("%EXTRA%", "")
    assert violations(base) == [], "对照臂:合成的干净 conf 必绿"
    srv = servers(parse(base))[0]
    assert emitted(srv, "/assets/x.js") is None, "不跳转的路径推演为不跳转"
    assert emitted(srv, "/sw.js") is None, "410 不是跳转"
    assert emitted(srv, "/cdn") == (301, "https://cdn.example.com/x"), "https 绝对目标原样"
    assert emitted(srv, "/fwd")[1].startswith("$http_x_forwarded_proto://"), "按转发协议生成的目标不误伤"
    # 牙证 ① 写死明文跳转目标
    bad = _SYNTH.replace("%EXTRA%", "location = /old { return 301 http://omnirank.top/new; }")
    assert any("写死了明文 scheme" in x for x in violations(bad))
    bad2 = _SYNTH.replace("%EXTRA%", "location = /old { return 301 $scheme://$host/new; }")
    assert any("写死了明文 scheme" in x for x in violations(bad2))
    # 牙证 ② location 里把 absolute_redirect 改回 on ⇒ 那条探针变绝对
    bad3 = base.replace("location = /social { return 302 /s; }",
                        "location = /social { absolute_redirect on; return 302 /s; }")
    v3 = violations(bad3)
    assert any("改回 on" in x for x in v3) and any(x.startswith("/social 发出") for x in v3), v3
    # 牙证 ③ 自动 301 只认「以 / 结尾的前缀 + *_pass」:把 /api/ 的 proxy_pass 拿掉 ⇒ /api 不再跳转(模型不乱报)
    no_pass = base.replace("location /api/ { proxy_pass http://backend; }", "location /api/ { root /app; }")
    assert emitted(servers(parse(no_pass))[0], "/api") is None
