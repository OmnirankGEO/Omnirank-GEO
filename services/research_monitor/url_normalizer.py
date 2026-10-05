"""
URL 归一化函数

把同一篇文章在不同站点 / 不同追踪参数下的多个 URL 归一为同一个,
防止 Jina 重复爬取浪费钱。
"""
import hashlib
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


# 要去掉的 host 前缀(忽略大小写)
HOST_PREFIXES_TO_STRIP = ('www.', 'm.', 'mobile.', 'wap.', '3g.', 'amp.')

# 要去掉的追踪参数前缀和具体参数名
TRACKING_PARAM_PREFIXES = ('utm_',)
TRACKING_PARAM_NAMES = {
    'fbclid', 'spm', 'ref', 'source', 'from', 'sourceid',
    'gclid', 'msclkid', 'mc_cid', 'mc_eid', 'igshid',
    '_hsenc', '_hsmi', 'hsCtaTracking',
}

# 协议白名单(防 javascript: data: ftp: 等)
ALLOWED_SCHEMES = ('http', 'https')


def normalize_url(url: str) -> str:
    """
    归一化 URL:
    1. host 转小写,去前缀(www/m/mobile/wap/3g/amp)
    2. 去追踪参数(utm_* 和列表里的)
    3. 去 #anchor
    4. 去末尾斜杠(根路径除外)

    异常场景(空字符串 / 非 URL / 非 http(s) scheme)返回空字符串,
    让上游过滤掉(防 SHA1 url_hash 与归一化合法 URL 落入不同行)。
    """
    if not url or not isinstance(url, str):
        return ""

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return ""

    if not parsed.scheme or not parsed.netloc:
        return ""

    # 协议白名单(防 javascript: data: ftp: 等)
    if parsed.scheme not in ALLOWED_SCHEMES:
        return ""

    host = parsed.netloc.lower()
    for prefix in HOST_PREFIXES_TO_STRIP:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break

    if parsed.query:
        kept_params = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            k_lower = k.lower()
            if any(k_lower.startswith(p) for p in TRACKING_PARAM_PREFIXES):
                continue
            if k_lower in TRACKING_PARAM_NAMES:
                continue
            kept_params.append((k, v))
        new_query = urlencode(kept_params)
    else:
        new_query = ''

    new_fragment = ''

    new_path = parsed.path
    if new_path.endswith('/') and new_path != '/':
        new_path = new_path.rstrip('/')

    return urlunparse((
        parsed.scheme,
        host,
        new_path,
        parsed.params,
        new_query,
        new_fragment,
    ))


def compute_url_hash(url: str) -> str:
    """
    计算 URL 的 SHA1 hex 指纹(40 字符),
    用于 geo_research_articles.url_hash 字段(UNIQUE 索引)。

    入参 url 应该已经经过 normalize_url() 归一化。
    """
    if not url or not isinstance(url, str):
        raise ValueError("url 不能为空或非 str")
    return hashlib.sha1(url.encode('utf-8')).hexdigest()
