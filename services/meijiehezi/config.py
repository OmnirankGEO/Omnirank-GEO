"""发布渠道配置(开源版空壳)。

开源版没有接入外部发布渠道:开关一律关、端点与域名一律空;只保留应用其它部分引用的常量名。
数值上限(标题长度、文件大小)是通用的内容约束,照常生效。
"""
SVIDEO_PUBLISH_ENABLED = False
SVIDEO_UPLOAD_ENABLED = False
ORDER_REMARK_ENABLED = False

#: 允许的素材地址域名:没有渠道就没有可信的渠道存储域。
SVIDEO_ALLOWED_MEDIA_HOSTS: tuple = ()

SVIDEO_MAX_VIDEO_BYTES = 1024 * 1024 * 1024
SVIDEO_MAX_IMAGE_BYTES = 10 * 1024 * 1024

SVIDEO_ARTICLE_TYPE_VIDEO = 1
SVIDEO_ARTICLE_TYPE_IMAGE = 3
SVIDEO_ALLOWED_ARTICLE_TYPES = (SVIDEO_ARTICLE_TYPE_VIDEO, SVIDEO_ARTICLE_TYPE_IMAGE)

SVIDEO_TITLE_HARD_LIMIT = 45
SVIDEO_TITLE_SOFT_LIMIT = 30

#: 渠道接口路径:开源版不接入任何渠道,这里是空表。
ENDPOINTS: dict = {}
#: 渠道确认码 → 确认字段:同上。
CONFIRM_CODE_MAP: dict = {}
