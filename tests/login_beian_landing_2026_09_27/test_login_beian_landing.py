# -*- coding: utf-8 -*-
"""omnirank.top 登录页备案号 / 口号 / 旧落地页下线(Review 09-27 · Owner「把社媒踢出去」· Deploy 盘点
C:/AI-Test/SITE_OMNIRANK_TOP_REUSE_AUDIT_2026-09-27.md)。

登录页是 omnirank.top 匿名访客唯一能看到的页面,按规定首页底部要挂备案号;口号只讲 GEO;
/landing 挂着作废的套餐价与「社媒操盘工具」,整页下线、跳登录页。

读源码树(不起前端):
  ① 登录页页脚(开源版):不写死任何备案号;构建时设了 VITE_ICP_BEIAN 才渲染,<a> 链 https://beian.miit.gov.cn/、
     新窗口(target=_blank + rel=noopener noreferrer)、文字就是那个值;
  ② 登录页口号:GEO 那一支恰为「AI 会推荐你吗?」(a4 推荐 · Review 批 · 官网 h1 后半句原样,半角问号);「刷得到」不再出现;
  ③ /landing:落到官网 https://omnirank.cn/(WO_293)—— 路由元素恰为 <ExternalRedirect to={OFFICIAL_SITE} />,
     OFFICIAL_SITE 恰为官网,ExternalRedirect 用 window.location.replace(不在历史里留 /landing);
     LandingPage 组件与目录都不在;全前端(注释行除外)没有别处链到 /landing
     (只许路由定义本身与三处「公开页」白名单判断 pathname === '/landing' / p === '/landing');
  ④ 旧落地页独有的「社媒操盘工具」在前端源码里 0 命中;
  ⑤ 开源页 public/opensource.html「访问 OmniRank 官网」链到官网 https://omnirank.cn/(WO_293)。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
LOGIN = SRC / "pages" / "Login" / "LoginPage.tsx"
APP = SRC / "App.tsx"
BEIAN_TEXT = "{import.meta.env.VITE_ICP_BEIAN}"
BEIAN_URL = "https://beian.miit.gov.cn/"
SLOGAN = "AI 会推荐你吗?"
OFFICIAL_SITE = "https://omnirank.cn/"
OPENSOURCE = ROOT / "frontend" / "public" / "opensource.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def beian_anchor_problems(src: str) -> list:
    """找登录页里的备案号 <a>;返回问题列表(空 = 合格)。"""
    anchors = re.findall(r"<a\b([^>]*)>\s*([^<]*?)\s*</a>", src, re.S)
    hits = [(attrs, text) for attrs, text in anchors if "beian" in attrs or "ICP" in text]
    if len(hits) != 1:
        return [f"备案号链接应恰有 1 个,实有 {len(hits)}"]
    attrs, text = hits[0]
    out = []
    if text != BEIAN_TEXT:
        out.append(f"文字应为 {BEIAN_TEXT},实为 {text!r}")
    if f'href="{BEIAN_URL}"' not in attrs:
        out.append("href 不是工信部备案系统")
    if 'target="_blank"' not in attrs:
        out.append("不是新窗口")
    rel = re.search(r'rel="([^"]*)"', attrs)
    if not rel or not {"noopener", "noreferrer"} <= set(rel.group(1).split()):
        out.append("rel 缺 noopener noreferrer")
    return out


def test_login_footer_has_the_icp_filing_link():
    src = _read(LOGIN)
    assert beian_anchor_problems(src) == []
    assert "粤ICP备" not in src, "开源版不许写死任何备案号"
    assert "{(import.meta.env.VITE_ICP_BEIAN || '').trim() && (" in src, "没设 VITE_ICP_BEIAN 就不渲染页脚备案号"
    # 牙证:检查器对几种坏形状都会喊
    assert beian_anchor_problems("<p>© 2026</p>")
    assert beian_anchor_problems(f'<a href="{BEIAN_URL}" target="_blank" rel="noopener noreferrer">粤ICP备2026002964号-3</a>')
    assert beian_anchor_problems(f'<a href="https://beian.gov.cn/" target="_blank" rel="noopener noreferrer">{BEIAN_TEXT}</a>')
    assert beian_anchor_problems(f'<a href="{BEIAN_URL}" rel="noopener noreferrer">{BEIAN_TEXT}</a>')


def test_login_slogan_is_geo_only():
    src = _read(LOGIN)
    assert f"'{SLOGAN}'" in src, "登录页口号应为 a4 定稿的「AI 会推荐你吗?」(半角问号,原样)"
    assert "刷得到" not in src


def test_landing_route_goes_to_the_official_site_and_the_page_is_gone():
    app = _read(APP)
    routes = re.findall(r'<Route\s+path="/landing"\s+element=\{(.*?)\}\s*/>[ \t]*$', app, re.M)   # 锚到行尾
    assert routes == ["<ExternalRedirect to={OFFICIAL_SITE} />"], routes
    assert re.findall(r"const OFFICIAL_SITE = '([^']*)';", app) == [OFFICIAL_SITE]
    body = re.search(r"function ExternalRedirect\(\{ to \}: \{ to: string \}\) \{(.*?)\n\}", app, re.S)
    assert body and "window.location.replace(to)" in body.group(1), "站外跳转要用 location.replace(不留历史)"
    assert not (SRC / "pages" / "Landing").exists()
    offenders = [str(p.relative_to(ROOT)) for p in SRC.rglob("*.ts*") if "LandingPage" in _read(p)]
    assert offenders == [], offenders


def test_nothing_else_links_to_landing():
    """全前端源码里 '/landing' 只许出现在:路由定义本身,与三处公开页白名单的等值判断。"""
    allowed = re.compile(r"""<Route\s+path="/landing"|(?:pathname|p)\s*===\s*'/landing'""")
    offenders = []
    comment = re.compile(r"\s*(//|/\*|\*|\{/\*)")
    for p in SRC.rglob("*.ts*"):
        for i, line in enumerate(_read(p).splitlines(), 1):
            if "/landing" in line and not allowed.search(line) and not comment.match(line):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:120]}")
    assert offenders == [], offenders


def test_retired_landing_content_is_gone_from_frontend_source():
    offenders = [str(p.relative_to(ROOT)) for p in SRC.rglob("*.ts*") if "社媒操盘工具" in _read(p)]
    assert offenders == [], offenders


def test_opensource_page_links_to_the_official_site():
    html = _read(OPENSOURCE)
    links = re.findall(r'<a href="([^"]*)">访问 OmniRank 官网</a>', html)
    assert links == [OFFICIAL_SITE], links
