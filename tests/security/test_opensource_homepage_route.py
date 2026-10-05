"""WO_277 · 开源署名条款写死的项目主页 https://omnirank.top/opensource 必须可达。

开源附加条件(开源/LICENSE_ADDITIONAL_CONDITION_CANONICAL.txt)要求再分发 / 部署时保留指向项目主页
https://omnirank.top/opensource 的链接 —— 这条 URL 落空,条款本身就指向一个 404。
改前:nginx 没有这条 location,落到 `location /` 的 SPA 兜底 → 前端「这个页面不存在」。
改后:`location = /opensource`(与带尾斜杠的 `/opensource/`)302 到临时说明页
`frontend/public/opensource.html`(vite 把 public/ 原样拷进 dist 根);仓库建好后只改 302 的目标。

锁一格(工单「路由存在 + 目标非 404」)+ 说明页内容一格。读的是仓里的 nginx.conf 与 frontend/public/,
它们就是镜像里 nginx 配置与 /app/frontend/dist 的来源。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NGINX = ROOT / "nginx.conf"
PUBLIC = ROOT / "frontend" / "public"
PATHS = ("/opensource", "/opensource/")


def _conf() -> str:
    """剥 `#` 注释(nginx 配置里 `#` 只做注释),判据只看指令;只取应用兜底块(`/opensource` 是应用那边的路由)。"""
    conf = "\n".join(line.split("#", 1)[0] for line in NGINX.read_text(encoding="utf-8").splitlines())
    return _server_block(conf, "server_name _;")


def _server_block(conf: str, name_line: str) -> str:
    """[WO_293] 容器 nginx 有两个 server 块(应用兜底 `_` + 官网 omnirank.cn),判据只看点名那一块:
    从含 `name_line` 的块的 `server {` 起按大括号配平截到它的 `}`。找不到 / 不唯一 ⇒ 红。"""
    assert conf.count(name_line) == 1, f"nginx.conf 里 `{name_line}` 应恰 1 处,读到 {conf.count(name_line)}"
    at = conf.index(name_line)
    start = conf.rindex("server {", 0, at)
    depth = 0
    for i in range(start, len(conf)):
        if conf[i] == "{":
            depth += 1
        elif conf[i] == "}":
            depth -= 1
            if depth == 0:
                return conf[start:i + 1]
    raise AssertionError("server 块没配平")


def _exact_blocks(conf: str, path: str) -> list[str]:
    return re.findall(r"location\s*=\s*" + re.escape(path) + r"\s*\{([^{}]*)\}", conf)


def _returns(block: str) -> list[tuple[str, str]]:
    return re.findall(r"return\s+(\d{3})\s+(\S+?)\s*;", block)


def test_opensource_homepage_route_exists_and_target_is_not_404() -> None:
    conf = _conf()
    targets = set()
    for path in PATHS:
        blocks = _exact_blocks(conf, path)
        assert len(blocks) == 1, f"nginx.conf 里 `location = {path}` 应恰好 1 个,读到 {len(blocks)} 个"
        rets = _returns(blocks[0])
        assert len(rets) == 1 and rets[0][0] == "302", f"`location = {path}` 应恰好一条 `return 302 <目标>;`,读到 {rets}"
        targets.add(rets[0][1])
        if rets[0][1].startswith("/"):
            # 源站只听 80:默认 absolute_redirect 会把 Location 拼成 http://…,边缘后面的 https 用户会被带回 http
            assert re.search(r"absolute_redirect\s+off\s*;", blocks[0]), f"`location = {path}` 站内 302 须 absolute_redirect off"
    assert len(targets) == 1, f"两条 location 应指向同一目标,读到 {sorted(targets)}"
    target = targets.pop()
    if target.startswith("https://"):
        return  # 建仓后:302 到仓库(外站),本仓验不到它的 200
    assert target.startswith("/") and not target.startswith("//"), f"站内目标须是站点根路径,读到 {target!r}"
    page = PUBLIC / target.lstrip("/")
    assert page.is_file() and page.stat().st_size > 0, f"302 目标 {target} 在 frontend/public/ 里不存在 ⇒ 线上 404"
    # 目标不许被别的 location 截走:不许有同名精确 location;`location /` 必须先按 $uri 找真文件
    assert not _exact_blocks(conf, target), f"目标 {target} 被一条精确 location 截走"
    root_blocks = re.findall(r"location\s+/\s*\{([^{}]*)\}", conf)
    assert len(root_blocks) == 1, "找不到唯一的 `location /`"
    assert re.search(r"root\s+/app/frontend/dist\s*;", root_blocks[0]), "`location /` 的 root 不是 /app/frontend/dist"
    assert re.search(r"try_files\s+\$uri\s", root_blocks[0]), "`location /` 不先按 $uri 找真文件 ⇒ 静态页会被 SPA 兜底吞掉"


def test_opensource_notice_page_content() -> None:
    conf = _conf()
    rets = _returns(_exact_blocks(conf, "/opensource")[0]) if _exact_blocks(conf, "/opensource") else []
    target = rets[0][1] if rets else ""
    if target.startswith("https://"):
        return  # 已改指仓库,说明页退场
    html = (PUBLIC / target.lstrip("/")).read_text(encoding="utf-8") if target.startswith("/") else ""
    body = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", body, flags=re.S))
    assert "OmniRank" in text and "全域上榜" in text, "说明页缺项目名(OmniRank / 全域上榜)"
    assert "开源版本即将发布" in text, "说明页缺「开源版本即将发布」"
    assert re.search(r'<a\s[^>]*href="[^"]+"[^>]*>[^<]*(反馈|联系)', body), "说明页缺联系入口"
    assert not re.search(r"20\d\d\s*[-/年.]|\d{1,2}\s*月\s*\d{1,2}\s*日|Q[1-4]\b", text), "说明页不许写日期(发布时间未定)"
    assert "<script" not in body.lower(), "说明页不依赖脚本(打开即读)"
