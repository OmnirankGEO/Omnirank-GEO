"""模拟发布客户端(PUBLISH_CHANNEL=dry_run)。

不访问任何外部地址:本模块不导入任何网络库,下单只在本地生成单号。
  · 目录:6 条软文演示媒体 + 2 条自媒体演示媒体,名字都带「(演示)」,价格是示例值,与任何真实报价无关;
  · 下单:每个媒体一个单号 ``DRY-<十六进制>``,立即受理;
  · 状态:由 services/publish_channels/status.py 推进 —— 受理满 N 分钟(PUBLISH_CHANNEL_DRY_RUN_DELAY_MINUTES,
    缺省 2)视为已发布,结果链接 ``about:blank#dry-run-<单号>``,不像、也不会被当成真链接;
  · 撤单:在途的单可以撤,应用照常把扣的算力原额退回。
短视频不在模拟范围内(短视频开关在开源版默认关闭)。
"""
from __future__ import annotations

import uuid

from services.meijiehezi.client import ChannelNotConfigured, PublishResult

ORDER_PREFIX = "DRY-"
RESULT_URL_PREFIX = "about:blank#dry-run-"
DEMO_TAG = "(演示)"

#: 演示目录。id 取 9 亿段,避开真实目录的主键区间;价格是示例值。
DEMO_ARTICLE_MEDIA = [
    {"id": 900000001 + i, "media_name": f"示例资讯站{i + 1}{DEMO_TAG}", "price": float(i + 1),
     "price1": float(i + 1), "price2": float(i + 1), "area": "全国", "portal_media": "示例门户",
     "resource_type_name": "新闻资讯", "inclusion_rate": 80, "publish_rate": "90%", "avg_time": 2,
     "remark": "模拟发布用的演示媒体,不会真的发出去", "case_link": ""}
    for i in range(6)
]
DEMO_WEMEDIA_MEDIA = [
    {"id": 900100001 + i, "toutiao_name": f"示例自媒体号{i + 1}{DEMO_TAG}", "platform": "示例平台",
     "price": float(i + 1), "price1": float(i + 1), "price2": float(i + 1), "fans_num": 1000 * (i + 1),
     "province": "全国", "industry": "综合", "remark": "模拟发布用的演示媒体,不会真的发出去"}
    for i in range(2)
]


def new_order_sn() -> str:
    return ORDER_PREFIX + uuid.uuid4().hex[:16].upper()


def result_url(order_sn: str) -> str:
    return RESULT_URL_PREFIX + order_sn


def _accept(media_ids) -> PublishResult:
    ids = [int(m) for m in media_ids]
    if not ids:
        return PublishResult(success=False, code=400, msg="没有选择媒体", raw_data={"mode": "dry_run"})
    sn_map = {mid: new_order_sn() for mid in ids}
    return PublishResult(success=True, code=200, msg="模拟发布已受理", selected_num=len(ids), success_count=len(ids),
                         order_sn=sn_map[ids[0]], raw_data={"mode": "dry_run"}, order_sn_map=sn_map)


def _page(rows: list, page: int, limit: int) -> tuple[int, list]:
    page, limit = max(1, int(page or 1)), max(1, int(limit or 50))
    return len(rows), [dict(r) for r in rows[(page - 1) * limit: page * limit]]


class DryRunClient:
    """与应用调用的那组方法同名同签名;所有方法都在本地完成。"""

    base_url = ""

    def __init__(self, *args, **kwargs):
        pass

    async def close(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def check_session(self) -> bool:
        return True

    async def refresh_token(self) -> None:
        return None

    # ---------------------------------------------------------------- 下单

    async def publish(self, title: str, content_md: str, media_ids: list, **_kwargs) -> PublishResult:
        return _accept(media_ids)

    async def publish_wemedia(self, title: str, content_md: str, toutiao_ids: list, **_kwargs) -> PublishResult:
        return _accept(toutiao_ids)

    async def publish_short_video(self, *args, **kwargs):
        raise ChannelNotConfigured()

    async def cancel_order(self, order_sn: str) -> bool:
        """撤单:模拟单在途时总能撤(应用侧只对待接单 / 发布中的单发起撤单)。"""
        return str(order_sn or "").startswith(ORDER_PREFIX)

    # ---------------------------------------------------------------- 目录

    async def get_media_list_raw(self, page: int = 1, limit: int = 50, **_filters) -> tuple[int, list]:
        return _page(DEMO_ARTICLE_MEDIA, page, limit)

    async def get_all_media_raw(self, limit: int = 50) -> list:
        return [dict(r) for r in DEMO_ARTICLE_MEDIA]

    async def get_wemedia_list_raw(self, page: int = 1, limit: int = 50, **_filters) -> tuple[int, list]:
        return _page(DEMO_WEMEDIA_MEDIA, page, limit)

    async def get_all_wemedia_raw(self, limit: int = 50) -> list:
        return [dict(r) for r in DEMO_WEMEDIA_MEDIA]

    async def get_short_video_list_raw(self, page: int = 1, limit: int = 50, **_filters) -> tuple[int, list]:
        return 0, []

    async def get_all_short_video_raw(self, limit: int = 50) -> list:
        return []
