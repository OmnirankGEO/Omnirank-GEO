"""
域名信誉分级

3 档:
- whitelist 白名单: 头部可信媒体(36kr / caixin / 财联社 / 新浪财经 等)
- blacklist 黑名单: 抖音 / 小红书 / B 站(短回答) + 短链域名(t.cn 等)
- gray 灰名单(默认): 其他

后期可扩展为从 DB 配置表动态加载,本版本写硬编码。
"""
from typing import Set


WHITELIST_DOMAINS: Set[str] = {
    '36kr.com',
    'caixin.com',
    'cls.cn',
    'sina.com.cn',
    'sina.com',
    'thepaper.cn',
    'jiemian.com',
    'huxiu.com',
    'pingwest.com',
    'tmtpost.com',
    'eeo.com.cn',
    'yicai.com',
    'nbd.com.cn',
    'stcn.com',
    'cnstock.com',
}

BLACKLIST_DOMAINS: Set[str] = {
    'douyin.com',
    'iesdouyin.com',
    'xiaohongshu.com',
    'bilibili.com',
    'b23.tv',
    'kuaishou.com',
    'weibo.com',
    'weibo.cn',
    'm.weibo.cn',
    't.cn',
    'dwz.cn',
    'url.cn',
    'sina.lt',
    'tinyurl.com',
}

HOST_PREFIXES = ('www.', 'm.', 'mobile.', 'wap.', '3g.', 'amp.')


def _normalize_domain(domain: str) -> str:
    """统一小写 + 去前缀 + 去 port/path(防御边界)"""
    if not domain:
        return ""
    d = domain.lower().strip()
    # 去端口和路径(防御边界)
    d = d.split(':')[0]   # 去 :8080
    d = d.split('/')[0]   # 去 /path
    for prefix in HOST_PREFIXES:
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d


def get_domain_tier(domain: str) -> str:
    """返回 'whitelist' / 'gray' / 'blacklist'"""
    d = _normalize_domain(domain)
    if not d:
        return "gray"
    if d in WHITELIST_DOMAINS:
        return "whitelist"
    if d in BLACKLIST_DOMAINS:
        return "blacklist"
    return "gray"


def is_blacklisted(domain: str) -> bool:
    """快捷判断: 是否黑名单"""
    return get_domain_tier(domain) == "blacklist"
