"""
GEO 调研监测 · 内容类型分类 + 平台中文名映射 子模块 [Phase 9 · 2026-05-25]

两个独立维度,跟现有 `services/research_monitor/domain_tiering.py` 不冲突:
    本模块  content_type   = 7 类(用途/类型维度)    video/article/doc_tool/encyc/ecom/gov/other
    现有    domain_tier    = 3 级(可信度维度)      whitelist/gray/blacklist
    可同时使用 — 一篇文章可以同时 (content_type='article', domain_tier='whitelist')

公共 API:
    get_content_type(domain, url='', text_chars=0) -> str   返回 7 类之一
    get_content_type_label(t) -> str                         返回中文标签
    get_platform_name(domain) -> str                         返回平台中文名(找不到返回原域名)
    CONTENT_TYPE_LABEL: dict                                  7 类英文 → 中文映射
    DOMAIN_TO_PLATFORM: dict                                  ~150 个主流域名 → 中文平台名

来源: 外部已验证(AI回答爬虫 2026-05-23 跑 16 行业 1.6 万篇文章)
"""

DOMAIN_TO_PLATFORM = {
    # 知乎
    "zhihu.com": "知乎",
    "zhuanlan.zhihu.com": "知乎专栏",
    # 搜狐
    "sohu.com": "搜狐",
    "news.sohu.com": "搜狐新闻",
    "tt.sohu.com": "搜狐号",
    "mp.sohu.com": "搜狐号",
    # CSDN
    "csdn.net": "CSDN",
    "blog.csdn.net": "CSDN 博客",
    "gitcode.csdn.net": "CSDN GitCode",
    "download.csdn.net": "CSDN 下载",
    # 百度
    "baidu.com": "百度",
    "baijiahao.baidu.com": "百度百家号",
    "baike.baidu.com": "百度百科",
    "mbd.baidu.com": "百度移动",
    "tieba.baidu.com": "百度贴吧",
    "zhidao.baidu.com": "百度知道",
    "wenku.baidu.com": "百度文库",
    "aiqicha.baidu.com": "百度爱企查",
    # 网易
    "163.com": "网易",
    "news.163.com": "网易新闻",
    "c.m.163.com": "网易移动",
    "dy.163.com": "网易订阅",
    "money.163.com": "网易财经",
    # 新浪
    "sina.com.cn": "新浪",
    "sina.cn": "新浪",
    "news.sina.com.cn": "新浪新闻",
    "tech.sina.com.cn": "新浪科技",
    "finance.sina.com.cn": "新浪财经",
    "finance.sina.cn": "新浪财经",
    "t.cj.sina.cn": "新浪财经",
    "blog.sina.com.cn": "新浪博客",
    # 腾讯
    "qq.com": "腾讯网",
    "news.qq.com": "腾讯新闻",
    "new.qq.com": "腾讯新闻",
    "tech.qq.com": "腾讯科技",
    "v.qq.com": "腾讯视频",
    "cloud.tencent.com": "腾讯云开发者",
    "cloud.tencent.cn": "腾讯云开发者",
    "mp.weixin.qq.com": "微信公众号",
    # 阿里
    "aliyun.com": "阿里云",
    "developer.aliyun.com": "阿里云开发者",
    "yunqi.aliyun.com": "阿里云栖",
    "help.aliyun.com": "阿里云文档",
    "alibaba.com": "阿里巴巴",
    "1688.com": "1688",
    "taobao.com": "淘宝",
    "tmall.com": "天猫",
    # 字节
    "toutiao.com": "今日头条",
    "ixigua.com": "西瓜视频",
    "douyin.com": "抖音",
    "developer.volcengine.com": "火山引擎开发者",
    "volcengine.com": "火山引擎",
    "feishu.cn": "飞书",
    # 凤凰
    "ifeng.com": "凤凰网",
    "tech.ifeng.com": "凤凰科技",
    "finance.ifeng.com": "凤凰财经",
    "news.ifeng.com": "凤凰新闻",
    # 主流媒体
    "jiemian.com": "界面新闻",
    "thepaper.cn": "澎湃新闻",
    "tidenews.com.cn": "潮新闻",
    "bjnews.com.cn": "新京报",
    "news.bjd.com.cn": "北京日报",
    "people.com.cn": "人民网",
    "xinhuanet.com": "新华网",
    "chinanews.com": "中新网",
    "ce.cn": "中国经济网",
    "cs.com.cn": "中证网",
    "yicai.com": "第一财经",
    "huxiu.com": "虎嗅",
    "36kr.com": "36氪",
    "geekpark.net": "极客公园",
    "leiphone.com": "雷锋网",
    "ithome.com": "IT之家",
    "it.ithome.com": "IT之家",
    "c.m.163.com": "网易移动",
    "tech.china.com": "中华网科技",
    "m.tech.china.com": "中华网科技",
    "china.com": "中华网",
    "chinadaily.com.cn": "中国日报",
    "tech.chinadaily.com.cn": "中国日报科技",
    "cnews.chinadaily.com.cn": "中国日报",
    # 财经
    "10jqka.com.cn": "同花顺",
    "news.10jqka.com.cn": "同花顺财经",
    "t.10jqka.com.cn": "同花顺",
    "eastmoney.com": "东方财富",
    "stcn.com": "证券时报",
    "hexun.com": "和讯网",
    "caixin.com": "财新网",
    "21jingji.com": "21 经济",
    "jrj.com.cn": "金融界",
    # 科技/开发者社区
    "cnblogs.com": "博客园",
    "juejin.cn": "稀土掘金",
    "segmentfault.com": "SegmentFault",
    "oschina.net": "开源中国",
    "gitee.com": "Gitee",
    "github.com": "GitHub",
    "stackoverflow.com": "StackOverflow",
    "51cto.com": "51CTO",
    "infoq.cn": "InfoQ",
    "ggm.com.cn": "拓墣产业研究院",
    # 内容/社区
    "bilibili.com": "B 站",
    "xiaohongshu.com": "小红书",
    "jianshu.com": "简书",
    "douban.com": "豆瓣",
    "weibo.com": "微博",
    "weibo.cn": "微博",
    # 数据/查询
    "qcc.com": "企查查",
    "tianyancha.com": "天眼查",
    "qichacha.com": "企查查",
    # 其他常见
    "zol.com.cn": "中关村在线",
    "pconline.com.cn": "太平洋电脑网",
    "g.pconline.com.cn": "太平洋电脑网",
    "tj91.com": "TJ91",
    "uqudao.com": "uqudao",
    "uweb.net.cn": "uweb",
    "youzan.com": "有赞",
    "uniquelogic.com": "Unique Logic",
    "hashmeta.ai": "Hashmeta",
    "stackmatix.com": "Stackmatix",
    "geneo.app": "Geneo",
    "sheepgeo.com": "SheepGEO",
    "baklib.com": "Baklib",
    "hayepusi.com": "Hayepusi",
    "seo.com": "SEO.com",
    "hsrb.com.cn": "黄山日报",
    "gelonghui.com": "格隆汇",
    "xhby.net": "新华日报",
    "hea.china.com": "中华网健康",
    # 垂直资讯/UGC 社区（被高频漏归 brand，统一补回 article 类）
    "smzdm.com": "什么值得买",
    "post.smzdm.com": "什么值得买",
    "post.m.smzdm.com": "什么值得买",
    "sspai.com": "少数派",
    "autohome.com.cn": "汽车之家",
    "chejiahao.autohome.com.cn": "汽车之家",
    "pcauto.com.cn": "太平洋汽车",
    "yiche.com": "易车",
    "dongchedi.com": "懂车帝",
    "yoojia.com": "有驾",
    "maigoo.com": "买购网",
    "to8to.com": "土巴兔",
    "chinapp.com": "中国品牌网",
    "m.chinapp.com": "中国品牌网",
    "jm.chinapp.com": "中国品牌网",
    "64365.com": "法律快车",
    "66law.cn": "66律师",
    "huichenglawyer.com": "汇成律师",
    "bohe.cn": "薄荷健康",
    "mfk.com": "妈妈帮",
    "mip.mfk.com": "妈妈帮",
    "cpic.com.cn": "中国太保",
    "shenlanbao.com": "深蓝保",
    "pingan.com": "平安",
    "cet.com.cn": "中信集团",
    "wandoujia.com": "豌豆荚",
    "cifnews.com": "雨果跨境",
    "xueqiu.com": "雪球",
    "book118.com": "原创力文档",
    "php.cn": "PHP 中文网",
    "eol.cn": "中国教育在线",
    "news.koolearn.com": "新东方在线",
    "mtoutiao.xdf.cn": "新东方头条",
    "qiantuxdf.cn": "新东方",
    "gaodun.com": "高顿教育",
    "zjjktv.com": "浙江健康TV",
    "licai.cofool.com": "理财吧",
    "global.lianlianpay.com": "连连国际",
    "gs.amazon.cn": "亚马逊全球开店",
    "baike.com": "互动百科",
    # 政府/官方
    "gov.cn": "政府门户",
    "sz.gov.cn": "深圳政府",
    "pnr.sz.gov.cn": "深圳规划和自然资源局",
    "jobui.com": "职友集",
    "lanyingim.com": "蓝莺 IM",
}


# ============== 内容类型分类 ==============
# 类型用 6 类（前端按需合并展示）:
#   video   : 视频/社媒（抖音、小红书、微博、B站、微信公众号、视频号、贴吧...）
#   article : 资讯/博客/UGC 长文（新闻门户、知乎专栏、CSDN、smzdm、百家号...）
#   encyc   : 百科/工具型（百度百科、维基、文库、知道）
#   ecom    : 电商/比价（京东、淘宝、天猫、拼多多、苏宁...）
#   gov     : 政府/教育/官方机构（.gov.cn、.edu.cn、政府门户）
#   brand   : 品牌官网 / 行业小站（不在已知映射表的域名 → 推断为厂家官网或行业小站）

import re

VIDEO_SOCIAL_DOMAINS = {
    # 抖音系
    "douyin.com", "iesdouyin.com",
    # 小红书
    "xiaohongshu.com", "xhslink.com",
    # 微博
    "weibo.com", "weibo.cn",
    # B 站
    "bilibili.com", "b23.tv",
    # 快手
    "kuaishou.com",
    # 西瓜视频（短视频社区）
    "ixigua.com",
    # 微信公众号 + 视频号
    "mp.weixin.qq.com", "channels.weixin.qq.com",
    # 贴吧（社区/UGC 短文）
    "tieba.baidu.com",
    # 腾讯视频
    "v.qq.com",
    # 海外短视频
    "youtube.com", "youtu.be", "tiktok.com",
}

# 视频/社媒 URL 路径特征（域名兜底，遇到也归视频）
VIDEO_PATH_HINTS = ("/share/video/", "/share/note/", "/note/", "/video/")

ENCYCLOPEDIA_DOMAINS = {
    "baike.baidu.com", "zhidao.baidu.com",
    "wenwen.sogou.com", "zh.wikipedia.org", "en.wikipedia.org",
    "baike.com",
}

# 文档/工具/资料站（独立一类：要么是文档预览/下载，要么是表单工具，不算文章也不算品牌）
DOC_TOOL_DOMAINS = {
    "renrendoc.com", "wenku.baidu.com",
    "book118.com", "max.book118.com", "mip.book118.com",
    "docin.com", "doc88.com", "360doc.com",
    "ixueshu.com", "xueshu.baidu.com",
    "wendangku.com", "wendangku.net",
    "doc.mbalib.com",
    "qikan.com",
}

ECOMMERCE_DOMAINS = {
    "jd.com", "tmall.com", "taobao.com", "1688.com",
    "pinduoduo.com", "yangkeduo.com",
    "suning.com", "vip.com", "kaola.com", "dangdang.com",
}

GOV_ORG_SUFFIX = (".gov.cn", ".edu.cn", ".org.cn", ".gov", ".edu")

# URL 路径含这些 → 强判文章（覆盖"未知域名→兜底"）
# 例: sohu.com/a/123456, qq.com/news/detail/789, mbachina.com/html/cjzx/202605/648972.html
_ARTICLE_PATH_PATTERNS = [
    re.compile(r"/articles?/"),
    re.compile(r"/news/"),
    re.compile(r"/paper/"),
    re.compile(r"/detail/"),
    re.compile(r"/post/"),
    re.compile(r"/p/\d"),
    re.compile(r"/a/\d"),
    re.compile(r"/n/\d"),
    re.compile(r"/info/"),
    re.compile(r"/content/"),
    re.compile(r"/show/\d"),
    re.compile(r"/zixun/"),
    re.compile(r"/cjzx/"),       # 财经资讯
    re.compile(r"/hltsci/"),     # 健康
    re.compile(r"/blog/"),
    re.compile(r"/item/\d"),
    re.compile(r"/html/.+\.html?$"),
    re.compile(r"/\d{6,}\.html?$"),  # /648972.html 等纯数字 ID 页
]

CONTENT_TYPE_LABEL = {
    "video":    "视频/社媒",
    "article":  "文章",
    "encyc":    "百科",
    "doc_tool": "文档/工具",
    "ecom":     "电商",
    "gov":      "政府/机构",
    "other":    "其他/官网",
}


def _strip_prefix(domain: str) -> str:
    for prefix in ("www.", "m.", "mobile.", "wap.", "3g."):
        if domain.startswith(prefix):
            return domain[len(prefix):]
    return domain


def _match_domain(domain: str, table) -> bool:
    """domain 完整或主域名兜底匹配 table (set/dict)"""
    if domain in table:
        return True
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in table:
            return True
    return False


def get_content_type(domain: str, url: str = "", text_chars: int = 0) -> str:
    """
    域名 + URL + 正文字数 → 内容类型 (7 类之一)
    判断优先级（高→低）:
        1. 政府/教育域后缀
        2. 视频/社媒(已知域)
        3. 视频(URL /share/video/ 等路径)
        4. 文档/工具站
        5. 百科
        6. 电商
        7. URL 含文章路径特征 (/article//news//paper//detail//数字.html ...)
        8. 已知媒体/平台映射表
        9. 正文 >= 500 字 → 长文必是文章(覆盖未知域名)
        10. 兜底 → other (其他/官网)
    """
    domain = (domain or "").lower().strip()
    url = (url or "").lower()
    if not domain:
        return "other"
    domain = _strip_prefix(domain)

    # 1. 政府/机构
    if domain.endswith(GOV_ORG_SUFFIX):
        return "gov"

    # 2. 视频/社媒（按域名）
    if _match_domain(domain, VIDEO_SOCIAL_DOMAINS):
        return "video"

    # 3. 视频/社媒（按 URL 路径特征兜底，仅对未知域名生效避免误伤新闻站的 /video/）
    if url and any(hint in url for hint in VIDEO_PATH_HINTS):
        if not _match_domain(domain, DOMAIN_TO_PLATFORM):
            return "video"

    # 4. 文档/工具
    if _match_domain(domain, DOC_TOOL_DOMAINS):
        return "doc_tool"

    # 5. 百科
    if _match_domain(domain, ENCYCLOPEDIA_DOMAINS):
        return "encyc"

    # 6. 电商
    if _match_domain(domain, ECOMMERCE_DOMAINS):
        return "ecom"

    # 7. URL 文章路径强信号 (覆盖"未知域名→兜底")
    if url and any(p.search(url) for p in _ARTICLE_PATH_PATTERNS):
        return "article"

    # 8. 已知平台/媒体映射表里 → 文章
    if _match_domain(domain, DOMAIN_TO_PLATFORM):
        return "article"

    # 9. 内容长度兜底：长文一定是文章（不管什么域名）
    if text_chars and text_chars >= 500:
        return "article"

    # 10. 真的兜底（短内容 + 未知域名 = 官网首页/落地页/不确定）
    return "other"


def get_content_type_label(t: str) -> str:
    return CONTENT_TYPE_LABEL.get(t, t)


def get_platform_name(domain: str) -> str:
    """域名 → 平台名。找不到就返回原域名"""
    domain = (domain or "").lower().strip()
    if not domain:
        return ""
    # 去掉 www / m / mobile / wap / 3g 前缀
    for prefix in ("www.", "m.", "mobile.", "wap.", "3g."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
            break
    # 完整匹配
    if domain in DOMAIN_TO_PLATFORM:
        return DOMAIN_TO_PLATFORM[domain]
    # 主域名兜底（去子域）
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in DOMAIN_TO_PLATFORM:
            return DOMAIN_TO_PLATFORM[candidate]
    return domain  # 完全没匹配上就保留原值
