"""开源版署名位的唯一开关与唯一文字来源(WO_329 · Owner 10-02 批「开源版的话可以加」)。

LICENSE 附加条件:对外提供服务 / 再分发时,不得移除、隐藏或修改界面、生成的报告 / 文档、客户门户页面上的
「OmniRank / 全域上榜」署名与指向项目主页的链接。本模块管这一行字出不出、出什么。

- ENABLED:主仓(线上我们自己的服务)写死 False —— 三处都不显示,白标照旧,行为与加这个模块之前逐字节一致。
  开源导出时由导出改写规则把下面那一行改成 True(C 的规则按整行匹配:行首无缩进、全文件只有这一处)。
  🔴 不读环境变量、不按「是不是生产」推断:开源用户部署的就是生产(a4 10-02)。
- 开着时白标不能隐藏这一行(许可证要求);样式可以调淡,但不许 display:none / visibility:hidden / 透明 / 零字号 / 移出视口。
- 三处(应用界面页脚 · 客户门户页脚 · 生成的报告 / 文档页脚)一律从本模块取字,不在各自页脚另写一遍。
文字定稿:开源/WO_329_OSS_ATTRIBUTION_LINE_a4_2026-10-02.md。
"""
from __future__ import annotations

ENABLED = True

HOMEPAGE = "https://omnirank.cn/opensource/"

#: 页面上(应用界面 · 客户门户)整行一个链接,指向 HOMEPAGE
UI_TEXT = "基于 OmniRank 开源版构建 · 全域上榜"
#: 英文界面
UI_TEXT_EN = "Built on OmniRank open source · 全域上榜"
#: 报告 / 文档页脚(打印出来点不了链接,所以把网址写出来;能放超链接的格式仍把整行链到 HOMEPAGE)
REPORT_TEXT = "基于 OmniRank 开源版构建 · 全域上榜 · omnirank.cn/opensource"


def payload() -> dict:
    """给前端与客户门户的那一份(关着时只回 enabled=False,不带字,页面什么都不渲染)。"""
    if not ENABLED:
        return {"enabled": False}
    return {"enabled": True, "text": UI_TEXT, "text_en": UI_TEXT_EN, "report_text": REPORT_TEXT, "href": HOMEPAGE}


def report_footer() -> str | None:
    """报告 / 文档页脚那一行;关着时 None(生成器原样不加)。"""
    return REPORT_TEXT if ENABLED else None


#: HTML 报告里那一行的 class(锁与导出闸按它找;样式只调淡,不隐藏)
REPORT_HTML_CLASS = "oss-attribution"


def report_footer_html() -> str:
    """HTML / PDF 报告页脚那一行;关着时空串 —— 调用方直接拼上,关着时产物与改动前逐字节一致。
    白标不经过这里:不管有没有白标、白标怎么配,开着就出这一行(许可证要求不得隐藏)。"""
    if not ENABLED:
        return ""
    return (f'<p class="{REPORT_HTML_CLASS}" style="margin:16px 0 0;font-size:11px;line-height:1.6;color:#6b7280;text-align:center">'
            f'<a href="{HOMEPAGE}" style="color:#6b7280;text-decoration:none">{REPORT_TEXT}</a></p>')
