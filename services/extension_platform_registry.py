"""浏览器扩展支持的平台注册表(**单一权威源**)。

原在插件后端(API 层),2026-08-19 R2 抽到 services 层 —— 因为
`services/publication_url_verifier.py` 要用这里的 `domains` 做**平台域清单**
(自报 URL 的域不在清单里,连"内容对上了"这种线索态都不给)。
留在 api 层会造成 services→api 的反向依赖,而插件后端本身又 import
verifier ⇒ 循环。抽出来是唯一不重复一份域名清单的做法(两份必然漂移)。

🔴 [WO_273 · 2026-09-23] 插件后端整体退役(原先它按同名 import 回去用这份清单)。
   本模块**保留**:在役消费方是核实器(`publication_url_verifier.py` 的域判定,核实器 cron
   仍在扫存量自报行)。名字里带 extension,但不是插件代码 —— 别按文件名把它当成退役对象删掉。
"""
from __future__ import annotations

from urllib.parse import urlsplit


EXTENSION_PLATFORMS = [
    {"id": "zhihu",     "name": "知乎",         "loginUrl": "https://www.zhihu.com/signin",                   "domains": ["zhihu.com"],                     "authCookies": ["z_c0"]},
    {"id": "csdn",      "name": "CSDN",         "loginUrl": "https://passport.csdn.net/login",                "domains": ["csdn.net"],                      "authCookies": ["UserToken", "UserInfo"]},
    {"id": "juejin",    "name": "掘金",         "loginUrl": "https://juejin.cn/login",                        "domains": ["juejin.cn"],                     "authCookies": ["sessionid"]},
    {"id": "baijiahao", "name": "百家号",       "loginUrl": "https://baijiahao.baidu.com/builder/rc/login",   "domains": ["baijiahao.baidu.com", "baidu.com"], "authCookies": ["BDUSS", "STOKEN"]},
    {"id": "weibo",     "name": "微博",         "loginUrl": "https://weibo.com/login.php",                    "domains": ["weibo.com", "sina.com.cn"],       "authCookies": ["SUB", "SUBP"]},
    {"id": "woshipm",   "name": "人人都是产品经理", "loginUrl": "https://www.woshipm.com/login",              "domains": ["woshipm.com"],                   "authCookies": []},
    {"id": "toutiao",   "name": "今日头条",     "loginUrl": "https://mp.toutiao.com/auth/page/login",         "domains": ["toutiao.com", "bytedance.com"],   "authCookies": ["sso_uid_tt", "uid_tt", "sso_uid_tt_ss", "uid_tt_ss"]},
    {"id": "sohu",      "name": "搜狐号",       "loginUrl": "https://mp.sohu.com/mpfe/v4/login",              "domains": ["sohu.com"],                      "authCookies": ["SUV", "SohuID", "gidinf"]},
    {"id": "douban",    "name": "豆瓣",         "loginUrl": "https://www.douban.com/accounts/login",          "domains": ["douban.com"],                    "authCookies": ["dbcl2", "ck"]},
    {"id": "cto51",     "name": "51CTO",        "loginUrl": "https://blog.51cto.com/login",                   "domains": ["51cto.com"],                     "authCookies": []},
    {"id": "cnblogs",   "name": "博客园",       "loginUrl": "https://passport.cnblogs.com/user/signin",       "domains": ["cnblogs.com"],                   "authCookies": [".CNBlogsCookie"]},
    {"id": "bilibili",  "name": "B站",          "loginUrl": "https://passport.bilibili.com/login",            "domains": ["bilibili.com"],                  "authCookies": ["SESSDATA", "bili_jct"]},
    {"id": "wechat",    "name": "微信公众号",   "loginUrl": "https://mp.weixin.qq.com",                       "domains": ["mp.weixin.qq.com"],              "authCookies": ["bizuin", "appmsg_token"]},
]

PLATFORM_NAMES = {p["id"]: p["name"] for p in EXTENSION_PLATFORMS}
PLATFORM_LOGIN_URLS = {p["id"]: p["loginUrl"] for p in EXTENSION_PLATFORMS}
PLATFORM_BY_ID = {p["id"]: p for p in EXTENSION_PLATFORMS}


#: platform 显示名 → id(publish_records.platform 存的是**显示名**,不是 id)
PLATFORM_IDS_BY_NAME = {p["name"]: p["id"] for p in EXTENSION_PLATFORMS}


def platform_domains(platform: str) -> tuple[str, ...]:
    """按 id 或显示名取该平台的合法域清单;认不出来的平台返回空元组。

    空元组的语义是**认不出来**,调用方必须当"不在清单里"处理,不能当"随便什么域都行"。
    """
    key = str(platform or "").strip()
    entry = PLATFORM_BY_ID.get(key) or PLATFORM_BY_ID.get(PLATFORM_IDS_BY_NAME.get(key, ""))
    return tuple(entry.get("domains", ())) if entry else ()


def url_is_within_platform_domain(platform: str, url: str) -> bool:
    """URL 的 host 是否落在该平台的域清单内(**按标签边界**匹配,不做子串)。

    🔴 子串匹配会把 `yoojia.com` 判成 `jia.com` 的子域(本仓踩过)。
       只认 host == domain 或 host 以 `.domain` 结尾。
    """
    domains = platform_domains(platform)
    if not domains:
        return False
    try:
        host = (urlsplit(str(url or "")).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    for raw in domains:
        d = str(raw or "").strip().lower().rstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False
