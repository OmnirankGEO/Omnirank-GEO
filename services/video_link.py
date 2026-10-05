"""视频链接基础解析:识别平台 · 抽分享链接 · 取视频 / 笔记详情(抖音 · 小红书 · B 站 · 视频号)。

E0b · 2026-09-26 从社媒工具包的一键仿写模块上提,逐字搬运(20 个顶层符号;原包已随开源 E3 · B2a 删除)。
在役调用方:`services/chat_attachments.py` · `tools/agent_loop/adapters/video.py`。
旧位置只留一行 re-export 兼容垫(标 E3 删),给包内尚未删除的社媒模块用。

🔴 本模块只做「取数」,不做改写:不许 import 任何改写 / 生成模块(one_click_rewrite、内容工坊、顾问 LLM)
   (锁见 tests/e0b_lift_scoring_video_2026_09_26,带牙证)。
🔴 E5(开源脱敏)待办:这里是 TikHub 取数代码,开源时要换成中性接口并提供 dry-run。
"""
import os
import re
import httpx
from typing import Dict, Any, Optional

from tools.asr.asr_tool import extract_audio_url_from_douyin
from tools.tikhub_cost_tracking import tracked_tikhub_get


TIKHUB_API_KEY = os.environ.get("TIKHUB_API_KEY")


TIKHUB_BASE_URL = "https://api.tikhub.io"


def detect_platform(url: str) -> str:
    """检测分享链接的平台"""
    url_lower = url.lower()

    if "v.douyin.com" in url_lower or "douyin.com" in url_lower:
        return "douyin"
    elif "xhslink" in url_lower or "xiaohongshu.com" in url_lower or "xhs" in url_lower:
        return "xiaohongshu"
    elif "channels.weixin.qq.com" in url_lower or "finder" in url_lower or "weixin.qq.com/sph" in url_lower:
        return "weixin"
    # 2026-05-09: 加 B 站识别 · tikhub 没 bilibili API · 后端走 friendly fallback 提示用户粘文案
    elif "bilibili.com" in url_lower or "b23.tv" in url_lower:
        return "bilibili"
    else:
        return "unknown"


def extract_share_url(text: str) -> Optional[str]:
    """从文本中提取分享链接"""
    # 抖音链接模式 - 支持字母数字下划线连字符
    douyin_patterns = [
        r'https?://v\.douyin\.com/[A-Za-z0-9_\-]+/?',
        r'https?://www\.douyin\.com/video/\d+',
    ]
    
    # 小红书链接模式
    xhs_patterns = [
        r'https?://xhslink\.com/[A-Za-z0-9_/]+',
        r'https?://www\.xiaohongshu\.com/discovery/item/[A-Za-z0-9]+',
        r'https?://www\.xiaohongshu\.com/explore/[A-Za-z0-9]+',
    ]
    
    # 视频号链接模式
    weixin_patterns = [
        r'https?://channels\.weixin\.qq\.com/web/pages/feed\?[^\s]+',
        # 2026-05-09: 视频号短链 / finder 域名
        r'https?://channels\.weixin\.qq\.com/[A-Za-z0-9/?=&_\-%\.]+',
        r'https?://finder\.video\.qq\.com/[A-Za-z0-9/?=&_\-%\.]+',
        # 2026-05-09 实测:老板真分享 sph 短链(weixin.qq.com/sph/XXX)
        r'https?://weixin\.qq\.com/sph/[A-Za-z0-9_\-]+/?',
    ]

    # 2026-05-09: B 站链接模式(detect 用 · 后端走 friendly fallback 提示粘文案)
    bilibili_patterns = [
        r'https?://b23\.tv/[A-Za-z0-9_\-]+/?',
        r'https?://www\.bilibili\.com/video/[A-Za-z0-9]+/?',
        r'https?://m\.bilibili\.com/video/[A-Za-z0-9]+/?',
    ]

    all_patterns = douyin_patterns + xhs_patterns + weixin_patterns + bilibili_patterns
    
    for pattern in all_patterns:
        match = re.search(pattern, text)
        if match:
            return match.group()
    
    return None


async def extract_video_id_douyin(share_url: str) -> Dict[str, Any]:
    """
    从抖音分享链接提取视频ID并获取完整视频数据
    
    两步流程:
    1. 从分享链接解析出 aweme_id（通过重定向或正则提取）
    2. 调用 /api/v1/douyin/app/v3/fetch_one_video_v3 获取完整数据（包含MP3）
    """
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            aweme_id = None
            
            # 方法1：先尝试通过重定向获取真实URL
            is_user_homepage = False
            sec_uid_from_url = None
            try:
                # 请求分享链接，获取重定向后的真实URL
                head_response = await client.head(share_url, follow_redirects=True)
                real_url = str(head_response.url)
                print(f"[REWRITE] 重定向URL: {real_url}")

                # 2026-05-18 老板截图复现 · v.douyin.com/FwSvn3eIA4k/ 重定向到 /share/user/MS4...
                # = 博主主页分享 不是单视频分享 · 此前 caller 收到通用"无法提取视频 ID"误以为 bug
                # 现在 detect 出来给 specific friendly error · 引导用户切到拆博主
                user_match = re.search(r'/share/user/([A-Za-z0-9_\-]+)', real_url)
                if user_match:
                    is_user_homepage = True
                    sec_uid_from_url = user_match.group(1)
                    print(f"[REWRITE] 检测到博主主页链接 sec_uid={sec_uid_from_url}")

                # 从真实URL中提取aweme_id (格式: /video/7597023840262196495)
                match = re.search(r'/video/(\d+)', real_url)
                if match:
                    aweme_id = match.group(1)
                    print(f"[REWRITE] 从重定向URL提取aweme_id: {aweme_id}")
                else:
                    # 备用：尝试 modal_id 参数
                    match = re.search(r'modal_id=(\d+)', real_url)
                    if match:
                        aweme_id = match.group(1)
                        print(f"[REWRITE] 从modal_id提取aweme_id: {aweme_id}")
            except Exception as e:
                print(f"[REWRITE] 重定向解析失败: {e}")
            
            # 方法2：如果重定向失败，尝试TikHub的分享链接解析API
            if not aweme_id:
                print("[REWRITE] 尝试TikHub分享链接解析API...")
                endpoints = [
                    "/api/v1/douyin/app/v3/fetch_one_video_by_share_url",
                    "/api/v1/douyin/web/fetch_one_video_by_share_url",
                ]
                for endpoint in endpoints:
                    try:
                        response = await tracked_tikhub_get(
                            client,
                            f"{TIKHUB_BASE_URL}{endpoint}",
                            caller="rewrite_douyin_share_url",
                            model="share_url",
                            metadata={"endpoint": endpoint},
                            headers={"Authorization": f"Bearer {TIKHUB_API_KEY}"},
                            params={"share_url": share_url}
                        )
                        if response.status_code == 200:
                            data = response.json()
                            aweme_info = data.get("data", {}).get("aweme_detail") or data.get("data", {})
                            if aweme_info and aweme_info.get("aweme_id"):
                                aweme_id = aweme_info.get("aweme_id")
                                print(f"[REWRITE] 从TikHub API提取aweme_id: {aweme_id}")
                                # 如果这个API返回了完整数据，直接使用
                                if aweme_info.get("video") or aweme_info.get("music"):
                                    return {
                                        "status": "success",
                                        "video_id": aweme_id,
                                        "sec_user_id": aweme_info.get("author", {}).get("sec_uid"),
                                        "full_data": aweme_info,
                                    }
                                break
                    except Exception as e:
                        print(f"[REWRITE] TikHub API {endpoint} 失败: {e}")
                        continue
            
            if not aweme_id:
                # 2026-05-18 老板截图复现 · 给 specific friendly error 引导用户切到正确 tab
                if is_user_homepage:
                    return {
                        "status": "error",
                        "error_code": "user_homepage_not_video",
                        "error": "这是博主主页分享链接·不是单条视频链接。请切到「拆博主」标签,或复制单个视频的分享链接(从视频右下角「分享」按钮拿)再粘进来。",
                        "sec_uid": sec_uid_from_url,
                        "suggest_switch_to": "author",
                    }
                return {
                    "status": "error",
                    "error_code": "video_id_extract_failed",
                    "error": "无法从分享链接提取视频ID·请确认是抖音单视频的分享链接(从视频右下角「分享」按钮拿)·不是博主主页/合集/直播间链接。",
                }

            # 步骤2：使用 REST API 多端点回退获取完整数据
            print(f"[REWRITE] 获取视频详情 aweme_id={aweme_id}")
            video_endpoints = [
                "/api/v1/douyin/app/v3/fetch_one_video",
                "/api/v1/douyin/app/v3/fetch_one_video_v2",
                "/api/v1/douyin/app/v3/fetch_one_video_v3",
                "/api/v1/douyin/web/fetch_one_video",
                "/api/v1/douyin/web/fetch_one_video_v2",
            ]
            last_status = 0
            for endpoint in video_endpoints:
                try:
                    print(f"[REWRITE] 尝试: {endpoint}")
                    resp = await tracked_tikhub_get(
                        client,
                        f"{TIKHUB_BASE_URL}{endpoint}",
                        caller="rewrite_douyin_video_detail",
                        model="video_detail",
                        metadata={"endpoint": endpoint},
                        headers={"Authorization": f"Bearer {TIKHUB_API_KEY}"},
                        params={"aweme_id": aweme_id}
                    )
                    last_status = resp.status_code
                    if resp.status_code == 200:
                        resp_data = resp.json()
                        aweme_detail = resp_data.get("data", {}).get("aweme_detail", {})
                        if aweme_detail:
                            print(f"[REWRITE] ✅ {endpoint} 成功")
                            return {
                                "status": "success",
                                "video_id": aweme_detail.get("aweme_id", aweme_id),
                                "sec_user_id": aweme_detail.get("author", {}).get("sec_uid"),
                                "full_data": aweme_detail,
                            }
                    print(f"[REWRITE] {endpoint} HTTP {resp.status_code}，继续回退...")
                except Exception as ep_err:
                    print(f"[REWRITE] {endpoint} 异常: {ep_err}")
                    continue

            return {"status": "error", "error": f"TikHub 所有视频接口均失败 (最后状态: {last_status})"}
                
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def fetch_video_detail_douyin(video_id: str) -> Dict[str, Any]:
    """
    获取抖音视频详情
    
    使用 TikHub API: /api/v1/douyin/app/v3/fetch_one_video
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await tracked_tikhub_get(
                client,
                f"{TIKHUB_BASE_URL}/api/v1/douyin/app/v3/fetch_one_video",
                caller="rewrite_douyin_video_detail",
                model="video_detail",
                headers={"Authorization": f"Bearer {TIKHUB_API_KEY}"},
                params={"aweme_id": video_id}
            )
            
            if response.status_code == 200:
                data = response.json()
                aweme_info = data.get("data", {}).get("aweme_detail", {})
                
                if not aweme_info:
                    return {"status": "error", "error": "No aweme_detail found"}
                
                # 使用统一的音频URL提取函数（与调研中心保持一致）
                # 优先级：视频URL > 音频URL > 音乐URL
                audio_url = extract_audio_url_from_douyin(aweme_info)
                stats_raw = aweme_info.get("statistics", {}) or {}

                return {
                    "status": "success",
                    "video_id": aweme_info.get("aweme_id"),
                    "title": aweme_info.get("desc", ""),
                    "author": {
                        "nickname": aweme_info.get("author", {}).get("nickname"),
                        "sec_user_id": aweme_info.get("author", {}).get("sec_uid"),
                        "follower_count": aweme_info.get("author", {}).get("follower_count", 0),
                    },
                    # NIT 3 (P1 探索发现):复盘场景需要 play / collect 来填表
                    # 抖音公开 API play_count 经常为 0(平台限制),但字段保留
                    "stats": {
                        "play": stats_raw.get("play_count", 0),
                        "digg": stats_raw.get("digg_count", 0),
                        "comment": stats_raw.get("comment_count", 0),
                        "collect": stats_raw.get("collect_count", 0),
                        "share": stats_raw.get("share_count", 0),
                    },
                    "duration_ms": aweme_info.get("duration", 0),
                    "audio_url": audio_url,
                    "video_url": f"https://www.douyin.com/video/{aweme_info.get('aweme_id')}",
                }
            else:
                return {"status": "error", "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def extract_xhs_note_id(share_url: str) -> Dict[str, Any]:
    """
    从小红书分享链接提取笔记ID + share_text(含 xsec_token)

    支持:
    - https://www.xiaohongshu.com/explore/xxx
    - https://www.xiaohongshu.com/discovery/item/xxx
    - https://xhslink.com/xxx (短链接，需要重定向)

    返回:
      {"status": "success", "note_id": "...", "share_text": "..."}
      share_text = redirect 后的真实 URL(含 xsec_token query)· 给 TikHub v7/v4 用
    """
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            note_id = None
            real_url = share_url  # 默认 share_text = 原链接

            # 直接从URL提取note_id
            match = re.search(r'/explore/([A-Za-z0-9]+)', share_url)
            if match:
                note_id = match.group(1)
                print(f"[XHS] 从explore URL提取note_id: {note_id}")

            if not note_id:
                match = re.search(r'/discovery/item/([A-Za-z0-9]+)', share_url)
                if match:
                    note_id = match.group(1)
                    print(f"[XHS] 从discovery URL提取note_id: {note_id}")

            # 短链接需要重定向
            # 2026-05-10:实测 xhslink.com/o/XXX 对 HEAD 返 404 · 对 GET 返 302
            #            改成 GET 跟随 · 兼容新老路径(/XXX 与 /o/XXX)
            if not note_id and "xhslink.com" in share_url:
                try:
                    head_response = await client.get(share_url, follow_redirects=True)
                    real_url = str(head_response.url)
                    print(f"[XHS] 重定向URL: {real_url}")

                    match = re.search(r'/explore/([A-Za-z0-9]+)', real_url)
                    if match:
                        note_id = match.group(1)
                    else:
                        match = re.search(r'/discovery/item/([A-Za-z0-9]+)', real_url)
                        if match:
                            note_id = match.group(1)

                    if note_id:
                        print(f"[XHS] 从重定向URL提取note_id: {note_id}")
                except Exception as e:
                    print(f"[XHS] 重定向解析失败: {e}")

            if not note_id:
                return {"status": "error", "error": "无法从链接提取小红书笔记ID"}

            # share_text 给 TikHub v7/v4 端点(它内部抽 xsec_token)
            # 如果有 redirect 后的真 URL(含 xsec_token query)· 用真 URL
            # 否则用原 share_url(直接粘的 explore/discovery 链接)
            return {"status": "success", "note_id": note_id, "share_text": real_url}

    except Exception as e:
        return {"status": "error", "error": str(e)}


def _parse_xhs_app_note_response(resp_json: dict, note_id: str) -> Dict[str, Any]:
    """
    解 TikHub /app/get_note_info triple wrap response(2026-05-10 Deploy-CTO 实证)

    schema:
      response.json()
        ├── code: 200, message_zh: "请求成功"  (TikHub 外层)
        └── data:
            ├── code: 0, success: true
            └── data: [{                    (★ array · 第 2 层)
                  "user": {"name": "...", "nickname": "...", "red_id": "..."},
                  "note_list": [{
                    "time": ...,
                    "mini_program_info": {
                      "title": "@xx 发了一篇笔记，快点来看吧！",  (含模板前缀)
                      "desc": "...",                                (笔记简介)
                      "thumb": "...",
                      "webpage_url": "..."
                    }
                  }]
                }]

    title 清模板前缀:"@xx 发了一篇笔记，快点来看吧！" → 提清后正文
    """
    outer = resp_json.get("data") or {}
    if not isinstance(outer, dict):
        return None
    if outer.get("code") not in (0, 200):
        return None
    inner_arr = outer.get("data") or []
    if not isinstance(inner_arr, list) or not inner_arr:
        return None
    item = inner_arr[0] or {}
    if not isinstance(item, dict):
        return None

    user = item.get("user") or {}
    note_list = item.get("note_list") or []
    if not isinstance(note_list, list) or not note_list:
        return None
    note = note_list[0] or {}
    if not isinstance(note, dict):
        return None

    mini = note.get("mini_program_info") or {}

    # title 清模板前缀("@xxx 发了一篇笔记，快点来看吧！" → 提取真正笔记内容引用)
    raw_title = mini.get("title", "") or ""
    cleaned_title = (
        raw_title
        .replace(" 发了一篇笔记，快点来看吧！", "")
        .replace(" 发了一篇笔记,快点来看吧！", "")
        .replace("@", "", 1)
        .strip()
    )
    desc = mini.get("desc", "") or ""

    if not cleaned_title and not desc:
        return None

    nickname = user.get("nickname") or user.get("name") or ""

    return {
        "id": note_id,
        "type": "video" if note.get("type") == "video" else "normal",
        "title": cleaned_title or desc[:30],
        "desc": desc,
        "tags": note.get("tag_list") or note.get("topics") or [],
        "images": note.get("images_list") or [],
        "image_count": len(note.get("images_list") or []),
        "author": {
            "nickname": nickname,
            "red_id": user.get("red_id", ""),
            "user_id": user.get("id", ""),
        },
        "stats": {
            "likes": note.get("liked_count") or 0,
            "collects": note.get("collected_count") or 0,
            "comments": note.get("comments_count") or 0,
            "shares": note.get("share_count") or 0,
        },
        "publish_time": note.get("time"),
        "webpage_url": mini.get("webpage_url", ""),
        "thumb": mini.get("thumb", ""),
    }


async def fetch_xhs_note_detail(note_id: str, share_text: str = "") -> Dict[str, Any]:
    """
    通过 TikHub API 获取小红书笔记详情(2026-05-10 真 schema)

    Deploy-CTO 实证 PROD:
    - /app/get_note_info ✅ 200(triple wrap data.data[0]·active)
    - /web/get_note_info_v* ❌ 全 400(TikHub web 端点已 broken)
    - /app/extract_share_info 拿 note_id + xsec_token(已经在 extract_xhs_note_id 解决)

    返:统一 schema(_parse_xhs_app_note_response 的返值)
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 优先 /app/get_note_info(active)· 备选 web 端点(broken 但留 fallback 防 TikHub 修好)
            endpoints = [
                ("/api/v1/xiaohongshu/app/get_note_info", {"note_id": note_id, "share_text": share_text or ""}),
                ("/api/v1/xiaohongshu/web/get_note_info_v7", {"note_id": note_id, "share_text": share_text or note_id}),
                ("/api/v1/xiaohongshu/web/get_note_info_v4", {"note_id": note_id, "share_text": share_text or note_id}),
            ]

            last_status = None
            for endpoint, params in endpoints:
                try:
                    response = await tracked_tikhub_get(
                        client,
                        f"{TIKHUB_BASE_URL}{endpoint}",
                        caller="rewrite_xhs_note_detail",
                        model="video_detail",
                        metadata={"endpoint": endpoint},
                        headers={"Authorization": f"Bearer {TIKHUB_API_KEY}"},
                        params=params,
                    )
                    last_status = response.status_code
                    if response.status_code != 200:
                        print(f"[XHS] {endpoint} 返 {response.status_code}")
                        continue

                    data = response.json()

                    # 优先 /app/ triple wrap parser
                    parsed = _parse_xhs_app_note_response(data, note_id)
                    if parsed:
                        print(f"[XHS] 成功获取笔记详情 via {endpoint}(triple wrap)")
                        return {"status": "success", **parsed}

                    # fallback:老 web schema(不规则嵌套)兼容残留路径
                    note_data = data.get("data", {})
                    if isinstance(note_data, dict) and "data" in note_data and not isinstance(note_data.get("data"), list):
                        note_data = note_data.get("data", {})
                    if isinstance(note_data, dict) and "items" in note_data:
                        items = note_data.get("items", [])
                        if items:
                            note_data = items[0].get("note", items[0]) if isinstance(items[0], dict) else items[0]
                    if isinstance(note_data, dict) and "note_list" in note_data:
                        notes = note_data.get("note_list", [])
                        if notes:
                            note_data = notes[0].get("note", notes[0]) if isinstance(notes[0], dict) else notes[0]

                    if isinstance(note_data, dict) and (note_data.get("title") or note_data.get("desc")):
                        print(f"[XHS] 成功获取笔记详情 via {endpoint}(legacy parser)")
                        return _parse_xhs_note(note_id, note_data)
                except Exception as e:
                    print(f"[XHS] {endpoint} 请求异常: {e}")
                    continue

            # 全 endpoint fail · 友好提示
            return {
                "status": "error",
                "error": (
                    f"小红书接口暂时拿不到笔记数据(last_status={last_status})· "
                    "请把笔记正文复制到这里(50 字以上)我帮你改成自己的话。"
                ),
            }

    except Exception as e:
        return {"status": "error", "error": str(e)}


def _parse_xhs_note(note_id: str, note_data: dict) -> Dict[str, Any]:
    """解析小红书笔记数据为结构化格式"""
    title = note_data.get("title") or note_data.get("display_title") or ""
    desc = note_data.get("desc") or note_data.get("note_desc") or ""

    # 提取标签
    tags = []
    tag_list = note_data.get("tag_list") or note_data.get("topics") or []
    for tag in tag_list:
        if isinstance(tag, dict):
            tag_name = tag.get("name") or tag.get("tag_name") or ""
            if tag_name:
                tags.append(tag_name)
        elif isinstance(tag, str):
            tags.append(tag)

    # 从描述中提取 #标签
    import re as _re
    hash_tags = _re.findall(r'#([^\s#]+)', desc)
    for ht in hash_tags:
        if ht not in tags:
            tags.append(ht)

    # 提取图片
    images = []
    image_list = note_data.get("image_list") or note_data.get("images") or []
    for img in image_list:
        if isinstance(img, dict):
            url = img.get("url") or img.get("url_default") or img.get("info_list", [{}])[0].get("url", "") if img.get("info_list") else ""
            if url:
                images.append(url)
        elif isinstance(img, str):
            images.append(img)

    # 判断笔记类型
    note_type = note_data.get("type", "normal")
    if note_type == "video" or note_data.get("video"):
        note_type = "video"
    else:
        note_type = "image_text"

    # 作者信息
    user = note_data.get("user", {})
    author = {
        "nickname": user.get("nickname") or user.get("name", ""),
        "user_id": user.get("userid") or user.get("user_id", ""),
        "follower_count": user.get("follower_count", 0),
    }

    # 统计数据
    stats = {
        "likes": note_data.get("liked_count") or note_data.get("likes", 0),
        "comments": note_data.get("comments_count") or note_data.get("comments", 0),
        "collects": note_data.get("collected_count") or note_data.get("collects", 0),
        "shares": note_data.get("shared_count") or note_data.get("shares", 0),
    }

    return {
        "status": "success",
        "note_id": note_id,
        "title": title,
        "desc": desc,
        "type": note_type,
        "tags": tags,
        "images": images,
        "image_count": len(images),
        "author": author,
        "stats": stats,
    }


def extract_bilibili_bv_id(share_url: str) -> Dict[str, Any]:
    """
    从 B 站分享链接提取 BV id

    支持:
    - https://www.bilibili.com/video/BV1S9dgBHELJ/?...
    - https://m.bilibili.com/video/BV1S9dgBHELJ
    - https://b23.tv/XXX (短链 · 需重定向 · 但路径匹配 BV id 时可直接抽)
    """
    # 直接 regex 抽 BV id(纯函数 · 不依赖网络)· 大部分场景 work
    bv_match = re.search(r'/video/(BV[A-Za-z0-9]+)', share_url)
    if bv_match:
        return {"status": "success", "bv_id": bv_match.group(1)}
    # b23.tv 短链 fallback:路径里没 BV id · 报错让上层走 GET redirect 兜底(后续可加)
    return {"status": "error", "error": "无法从 B 站链接提取 BV id · 请用 bilibili.com/video/BVXXX 完整链接"}


def _parse_bilibili_video_detail(resp_json: dict) -> dict:
    """
    解 TikHub fetch_one_video 双层 wrap response
    实证 prod(2026-05-10 Deploy-CTO):
    {"code":200,"data":{"code":0,"data":{"bvid":"...","title":"...","aid":...,"cid":...,...}}}
    返回扁平化的 inner dict(若双层都缺则返 {})
    """
    outer = resp_json.get("data") or {}
    if not isinstance(outer, dict):
        return {}
    # 双层 wrap:外 data 包 bilibili API 内 data
    inner = outer.get("data") if isinstance(outer.get("data"), dict) else outer
    return inner if isinstance(inner, dict) else {}


def _select_chinese_subtitle_url(resp_json: dict) -> str:
    """
    解 TikHub fetch_video_subtitle 单层 wrap response · 选 ai-zh 字幕 · 补 https: 前缀
    实证 prod:{"code":200,"data":{"subtitles":[{"lan":"ai-zh","subtitle_url":"//aisubtitle.hdslb.com/..."}]}}
    无中文字幕返 ""
    """
    data = resp_json.get("data") or {}
    if not isinstance(data, dict):
        return ""
    subtitles = data.get("subtitles") or []
    if not isinstance(subtitles, list):
        return ""
    # 优先 ai-zh(AI 中文字幕)· 次 zh-Hans · 最后任意中文
    candidates = []
    for s in subtitles:
        if not isinstance(s, dict):
            continue
        lan = (s.get("lan") or "").lower()
        url = s.get("subtitle_url") or s.get("subtitle_url_v2") or ""
        if not url or not isinstance(url, str):
            continue
        if lan == "ai-zh":
            candidates.insert(0, url)  # 最高优先
        elif "zh" in lan and "en" not in lan:
            candidates.append(url)
    if not candidates:
        return ""
    url = candidates[0]
    if url.startswith("//"):
        url = "https:" + url
    return url


def _parse_bilibili_subtitle_body(subtitle_json: dict) -> str:
    """
    第二步 GET subtitle_url 拿到字幕 JSON · 拼 body[*].content 成纯文本
    bilibili 标准字幕 schema:{"body":[{"from":0.0,"to":2.5,"content":"..."},...]}
    """
    body = subtitle_json.get("body") or []
    if not isinstance(body, list):
        return ""
    lines = []
    for line in body:
        if isinstance(line, dict):
            txt = line.get("content", "") or line.get("text", "")
            if txt:
                lines.append(str(txt))
    return "\n".join(lines)


async def fetch_bilibili_video_detail(bv_id: str) -> Dict[str, Any]:
    """
    通过 TikHub API 获取 B 站视频详情 + 字幕(transcript)

    实测 prod 真 schema(2026-05-10 Deploy-CTO):
    步骤 1:GET /api/v1/bilibili/web/fetch_one_video?bv_id=...
            response 双层 wrap · data.data.* 含 title/desc/aid/cid/owner/stat/pic/duration
    步骤 2:GET /api/v1/bilibili/web/fetch_video_subtitle?a_id=...&c_id=...
            response 单层 wrap · data.subtitles[].subtitle_url(// 开头需补 https:)
    步骤 3:GET subtitle_url(直接 bilibili CDN · 不走 TikHub)
            返回 {"body": [{"content":"..."}]} · 拼 content 成 transcript
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # 步骤 1:基础详情(双层 wrap)
            r1 = await tracked_tikhub_get(
                client,
                f"{TIKHUB_BASE_URL}/api/v1/bilibili/web/fetch_one_video",
                caller="rewrite_bilibili_video_detail",
                model="video_detail",
                headers={"Authorization": f"Bearer {TIKHUB_API_KEY}"},
                params={"bv_id": bv_id},
            )
            if r1.status_code != 200:
                return {"status": "error", "error": f"TikHub bilibili fetch_one_video 返回 {r1.status_code}"}

            inner = _parse_bilibili_video_detail(r1.json())
            title = (inner.get("title") or "")
            description = (inner.get("desc") or "")
            aid = inner.get("aid")
            cid = inner.get("cid")
            owner = inner.get("owner") or {}
            stat = inner.get("stat") or {}
            tags_raw = inner.get("tags") or inner.get("tag") or []
            tags = [t.get("tag_name", "") if isinstance(t, dict) else str(t) for t in tags_raw if t][:10]

            if not title and not description:
                return {"status": "error", "error": "B 站 API 返回数据为空 · 链接可能已失效"}

            # 步骤 2 + 3:字幕(可能没字幕 · 不算 fail)
            subtitle_text = ""
            if aid and cid:
                try:
                    r2 = await tracked_tikhub_get(
                        client,
                        f"{TIKHUB_BASE_URL}/api/v1/bilibili/web/fetch_video_subtitle",
                        caller="rewrite_bilibili_subtitle",
                        model="video_detail",
                        headers={"Authorization": f"Bearer {TIKHUB_API_KEY}"},
                        params={"a_id": str(aid), "c_id": str(cid)},
                    )
                    if r2.status_code == 200:
                        sub_url = _select_chinese_subtitle_url(r2.json())
                        if sub_url:
                            r3 = await client.get(sub_url, follow_redirects=True)
                            if r3.status_code == 200:
                                subtitle_text = _parse_bilibili_subtitle_body(r3.json())
                                print(f"[Bilibili] 字幕拉取成功 · {len(subtitle_text)} 字")
                            else:
                                print(f"[Bilibili] 字幕 CDN 返 {r3.status_code} · 降级")
                        else:
                            print(f"[Bilibili] 没中文字幕 · 降级用标题+描述")
                except Exception as sub_e:
                    print(f"[Bilibili] 字幕获取失败(降级): {sub_e}")

            return {
                "status": "success",
                "bv_id": bv_id,
                "title": title[:200],
                "description": description,
                "author": {"nickname": owner.get("name", ""), "uid": owner.get("mid")},
                "tags": tags,
                "stats": {
                    "view": stat.get("view", 0),
                    "danmaku": stat.get("danmaku", 0),
                    "reply": stat.get("reply", 0),
                    "favorite": stat.get("favorite", 0),
                    "coin": stat.get("coin", 0),
                    "share": stat.get("share", 0),
                    "like": stat.get("like", 0),
                },
                "subtitle_text": subtitle_text,
            }

    except Exception as e:
        return {"status": "error", "error": f"B 站详情获取失败: {e}"}


def format_bilibili_video_as_text(video_detail: dict) -> str:
    """B 站 detail → transcript-like 文本(标题 + 描述 + 字幕 + 标签)"""
    parts = []

    title = video_detail.get("title", "")
    if title:
        parts.append(f"【标题】{title}")

    description = video_detail.get("description", "")
    if description:
        parts.append(f"\n【描述】\n{description}")

    subtitle_text = video_detail.get("subtitle_text", "")
    if subtitle_text:
        parts.append(f"\n【口播字幕】\n{subtitle_text}")

    tags = video_detail.get("tags", [])
    if tags:
        parts.append(f"\n【标签】{' '.join('#' + t for t in tags if t)}")

    stats = video_detail.get("stats", {})
    if any(stats.values()):
        parts.append(
            f"\n【数据】播放 {stats.get('view', 0)} | 点赞 {stats.get('like', 0)} | "
            f"投币 {stats.get('coin', 0)} | 收藏 {stats.get('favorite', 0)} | 弹幕 {stats.get('danmaku', 0)}"
        )

    author = video_detail.get("author", {})
    if author.get("nickname"):
        parts.append(f"\n【作者】{author.get('nickname', '')}")

    parts.append(f"\n【平台】B 站(Bilibili)")
    return "\n".join(parts)


async def extract_wechat_video_id(share_url: str) -> Dict[str, Any]:
    """
    从视频号分享链接提取 video ID

    重要(2026-05-09 实测):
    - sph 短链(weixin.qq.com/sph/XXX)· 重定向后 URL 含 finder-preview/pages/sph?id=XXX
      这个 id 实际是 **exportId** · TikHub API 必须用 exportId 参数才能 200 OK
    - export_id= 显式参数 也是 exportId
    - /feed/XXX 路径式 才是真正的永久 id
    """
    try:
        # 2026-05-10 兜底优先:sph 短链路径直接 regex 抽短码 = exportId
        # 实测 prod 微信服务器对 server-side HTTP HEAD 不一定返 redirect · 跟用浏览器/curl 不同
        # 所以不依赖网络重定向 · 直接从 URL 路径提
        sph_path_match = re.search(r'/sph/([A-Za-z0-9_\-]+)', share_url)
        if sph_path_match:
            return {"status": "success", "video_id": sph_path_match.group(1), "id_type": "exportId"}

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            target_url = share_url

            # 短链跟随重定向拿真实 URL(老 channels.weixin.qq.com 短链路径仍走这里)
            if "channels.weixin.qq.com" in share_url and "?" not in share_url:
                try:
                    head_response = await client.head(share_url, follow_redirects=True)
                    target_url = str(head_response.url)
                    print(f"[Wechat] 重定向 URL: {target_url}")
                except Exception as e:
                    print(f"[Wechat] 重定向失败: {e}")

            is_sph_path = ("/sph/" in target_url) or ("finder-preview/pages/sph" in target_url)

            # 路径式 /feed/XXX = 永久 id
            path_match = re.search(r'/feed/([A-Za-z0-9_\-]+)', target_url)
            if path_match:
                return {"status": "success", "video_id": path_match.group(1), "id_type": "id"}

            # 显式 export_id= 参数(老式 share)
            export_match = re.search(r'[?&]export_id=([A-Za-z0-9_\-]+)', target_url)
            if export_match:
                return {"status": "success", "video_id": export_match.group(1), "id_type": "exportId"}

            # ?id=XXX 参数 · sph 路径下当 exportId 用(实测) · 其他路径当 id
            id_match = re.search(r'[?&]id=([A-Za-z0-9_\-]+)', target_url)
            if id_match:
                return {
                    "status": "success",
                    "video_id": id_match.group(1),
                    "id_type": "exportId" if is_sph_path else "id",
                }

            return {"status": "error", "error": "无法从视频号链接提取视频 ID · 请确认链接完整"}

    except Exception as e:
        return {"status": "error", "error": str(e)}


async def fetch_wechat_video_detail(video_id: str, id_type: str = "exportId") -> Dict[str, Any]:
    """
    通过 TikHub API 获取视频号视频详情

    GET /api/v1/wechat_channels/fetch_video_detail
    query: id (优先) 或 exportId(fallback · 会过期)

    Returns:
        {
            "status": "success",
            "video_id": "...",
            "title": "...",
            "description": "...",
            "author": {"nickname": "..."},
            "stats": {...},
            "tags": [...],
        }
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {id_type: video_id} if id_type == "exportId" else {"id": video_id}
            response = await tracked_tikhub_get(
                client,
                f"{TIKHUB_BASE_URL}/api/v1/wechat_channels/fetch_video_detail",
                caller="rewrite_wechat_video_detail",
                model="video_detail",
                headers={"Authorization": f"Bearer {TIKHUB_API_KEY}"},
                params=params,
            )
            if response.status_code != 200:
                return {"status": "error", "error": f"TikHub 视频号 API 返回 {response.status_code}"}

            data = response.json()
            inner = data.get("data") or {}
            obj = inner.get("object_desc") or {}

            # 字段映射 · TikHub 返回结构(基于 spec)
            title = obj.get("description") or obj.get("title") or ""
            description = obj.get("description") or ""
            # 作者
            author = obj.get("nickname") or (inner.get("nickname")) or ""
            sec_uid = inner.get("username") or obj.get("username") or ""
            # 标签 / 话题
            tags_raw = obj.get("topic") or []
            tags = [t.get("name", "") if isinstance(t, dict) else str(t) for t in tags_raw if t]
            # stats
            extra_info = inner.get("extra_info") or {}
            stats = {
                "like": extra_info.get("like_count") or 0,
                "comment": extra_info.get("comment_count") or 0,
                "share": extra_info.get("share_count") or 0,
                "favorite": extra_info.get("favorite_count") or 0,
                "play": extra_info.get("play_count") or 0,
            }

            if not title and not description:
                return {"status": "error", "error": "视频号 API 返回数据为空 · 链接可能已失效"}

            # 加密 mp4 媒体 · ASR 链路用(url + url_token + decode_key 三者一起 · 每次新发)
            media_list = obj.get("media") or []
            media_first = media_list[0] if media_list else {}
            media_payload = {
                "url": media_first.get("url", ""),
                "url_token": media_first.get("url_token", ""),
                "decode_key": media_first.get("decode_key", ""),
                "file_size": media_first.get("file_size", 0),
            } if media_first else {}

            return {
                "status": "success",
                "video_id": video_id,
                "title": title[:200],
                "description": description,
                "author": {"nickname": author, "sec_user_id": sec_uid},
                "tags": [t for t in tags if t][:10],
                "stats": stats,
                "media": media_payload,
            }

    except Exception as e:
        return {"status": "error", "error": f"视频号详情获取失败: {e}"}


def format_wechat_video_as_text(video_detail: dict) -> str:
    """视频号 detail → transcript-like 文本(标题 + 描述 + 标签 + 作者 + stats)"""
    parts = []

    title = video_detail.get("title", "")
    if title:
        parts.append(f"【标题】{title}")

    description = video_detail.get("description", "")
    if description and description != title:
        parts.append(f"\n【描述】\n{description}")

    tags = video_detail.get("tags", [])
    if tags:
        parts.append(f"\n【话题】{' '.join('#' + t for t in tags)}")

    stats = video_detail.get("stats", {})
    if any(stats.values()):
        parts.append(
            f"\n【数据】播放 {stats.get('play', 0)} | 点赞 {stats.get('like', 0)} | "
            f"评论 {stats.get('comment', 0)} | 分享 {stats.get('share', 0)}"
        )

    author = video_detail.get("author", {})
    if author.get("nickname"):
        parts.append(f"\n【作者】{author.get('nickname', '')}")

    parts.append(f"\n【平台】视频号(微信 Channels)")
    return "\n".join(parts)


def format_xhs_note_as_text(note_detail: dict) -> str:
    """将小红书笔记数据格式化为结构化文本，用于LLM分析和仿写"""
    parts = []

    title = note_detail.get("title", "")
    if title:
        parts.append(f"【标题】{title}")

    desc = note_detail.get("desc", "")
    if desc:
        parts.append(f"\n【正文】\n{desc}")

    tags = note_detail.get("tags", [])
    if tags:
        parts.append(f"\n【标签】{' '.join('#' + t for t in tags)}")

    note_type = note_detail.get("type", "image_text")
    image_count = note_detail.get("image_count", 0)
    if note_type == "image_text" and image_count > 0:
        parts.append(f"\n【笔记类型】图文笔记（{image_count}张图片）")
    elif note_type == "video":
        parts.append(f"\n【笔记类型】视频笔记")

    stats = note_detail.get("stats", {})
    if any(stats.values()):
        parts.append(f"\n【数据】点赞 {stats.get('likes', 0)} | 收藏 {stats.get('collects', 0)} | 评论 {stats.get('comments', 0)} | 分享 {stats.get('shares', 0)}")

    author = note_detail.get("author", {})
    if author.get("nickname"):
        parts.append(f"【作者】{author.get('nickname', '')}")

    return "\n".join(parts)
