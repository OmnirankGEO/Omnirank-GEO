"""[短视频尾巴包] 两个锁：上传提前应答必须先 drain；媒体名必须来自服务端权威表。

两条都必须**双向**锁：
  - 只锁「错误路径返 4xx」→ 把端点改成无条件 4xx 也全绿；
  - 只锁「正常路径能传」→ 把 drain 删掉也全绿。
所以每条都配一个反向用例。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = (ROOT / "api" / "meijiehezi_api.py").read_text(encoding="utf-8")
DB = (ROOT / "db" / "meijiehezi_db.py").read_text(encoding="utf-8")


def _upload_video_src() -> str:
    start = API.index("async def api_short_video_upload_video")
    rest = API[start + 10:]
    nxt = re.search(r"\n@router\.", rest)
    return API[start: start + 10 + (nxt.start() if nxt else len(rest))]


# ─────────────────────────── 锁 1：提前应答必须 drain ───────────────────────────

def test_all_early_rejects_go_through_drain_helper():
    """闸/扩展名/大小 四个早退分支，必须全部走 drain 后再抛，不能有裸 raise。

    🔴 这是 502 的根因：nginx 对本路由关了请求体缓冲，上游在客户端发完前应答
      → 连接无法收尾 → nginx 回 502，我们写的文案被吞掉。
    """
    src = _upload_video_src()
    # 定位到「开始真正读流」之前的那一段（校验区）
    cut = src.index("client = _get_client()")
    guard_zone = src[:cut]

    assert "async def _reject(" in guard_zone, "缺少 drain 助手 _reject"
    assert "async for _ in request.stream():" in guard_zone, "_reject 必须真的把请求体读空"

    # 🔴 助手体内那句 raise 是**应该有的**（drain 完再抛），必须先剔除再查，
    #   否则锁会把正确实现判红（第一版就是这么误报的）。
    helper_start = guard_zone.index("async def _reject(")
    helper_end = guard_zone.index("_get_user(request)")
    checked = guard_zone[:helper_start] + guard_zone[helper_end:]

    # 校验区（助手体之外）不允许出现裸 raise HTTPException —— 必须走 _reject
    bare = re.findall(r"^\s*raise HTTPException", checked, re.M)
    assert not bare, (
        f"校验区仍有 {len(bare)} 处裸 raise HTTPException —— 大文件下会变成 502 而不是可读文案")

    # 四个早退分支都在
    for kw in ("短视频上传即将开放", "换个 MP4 或 FLV", "视频是空的", "视频有点大"):
        assert kw in guard_zone, f"早退分支文案缺失: {kw}"
        # 每条文案都必须由 _reject 送出
        assert re.search(r"await _reject\([^)]*" + re.escape(kw[:6]), guard_zone), \
            f"文案「{kw}」没走 _reject，仍是提前应答"


def test_reject_helper_swallows_stream_errors():
    """drain 时客户端已断开是常态，不能因此把 500 抛给用户（本来就要返错）。"""
    src = _upload_video_src()
    helper = src[src.index("async def _reject("): src.index("_get_user(request)")]
    assert "except Exception" in helper, "drain 必须吞掉流异常，否则客户端断开会变成 500"


def test_reverse_normal_path_still_streams_to_relay():
    """反向锁：正常路径必须仍把 request.stream() 交给中转，drain 不能把它吃掉。

    没有这一条，把整个端点改成「无条件 drain + 400」也能让上面的锁全绿。
    """
    src = _upload_video_src()
    assert re.search(r"relay_short_video_upload\([^)]*chunks=request\.stream\(\)", src, re.S), \
        "正常路径必须把 request.stream() 传给 relay —— drain 改动不得影响主路径"
    # drain 只能出现在 _reject 里，主路径不得先把流读空
    main = src[src.index("client = _get_client()"):]
    assert "async for _ in" not in main, "主路径不得排空请求体（那会把文件本身丢掉）"


def test_size_check_still_before_body_read():
    """长度校验仍必须在读 body 之前 —— 修 502 不能把这条设计改回去。"""
    src = _upload_video_src()
    assert src.index("content-length") < src.index("relay_short_video_upload"), \
        "长度校验必须在真正读流之前，否则 900MB 的错误文件会白占满带宽"


# ─────────────────────── 锁 2：媒体名以服务端权威表为准 ───────────────────────

def test_media_name_comes_from_server_table():
    """下单时 media_name 必须查库，客户端值只作兜底。"""
    assert "def _resolve_svideo_media_names" in API, "缺少服务端取名 helper"
    assert "FROM mhz_short_video WHERE id IN" in API, "必须从权威表 mhz_short_video 取名"

    start = API.index('"media_type": MEDIA_TYPE_SVIDEO')
    block = API[max(0, start - 700): start]
    assert "_name_map.get(int(mid))" in block, "media_name 必须优先取服务端查到的名字"
    assert re.search(r"_name_map\.get\(int\(mid\)\)\s*or\s*_client_name", block), \
        "客户端值只能作兜底（or 右侧），不能反过来"


def test_media_name_lookup_is_fail_soft():
    """取名字失败绝不能阻断下单 —— 名字是显示问题，钱和发布才是主线。

    🔴 断言必须打在 **except 块内部**：只查「helper 里有 except 和 return {}」是抓不住的，
      因为函数开头「空 ids 提前 return {}」那句会让断言恒真（第一版变异 M5 就是这么存活的）。

    🔴 切片**不依赖两个函数的定义顺序**（2026-08-02 去重返工时踩到）：
      本包原实现排在 `_recompute_publish_charge` 之前，生产版排在它之后 ——
      沿用「切到 _recompute_publish_charge 为止」会切出**反向区间**（起点在终点之后），
      得到空串，于是 `except` 相关断言全部恒真，锁静默失效。
      改成「切到它后面的下一个顶层 def」，与谁在前谁在后无关。
      也**不写死**下一个函数叫什么（当前实测是 `_publish_approval_gate`）——
      写死等于把这把锁绑在一个无关函数的位置上，那函数一改名锁就废。
    """
    _s = API.index("def _resolve_svideo_media_names")
    _e = API.index("\ndef ", _s + 1)          # 它后面的下一个顶层 def
    helper = API[_s:_e]
    assert len(helper) > 200, "切片为空/过短 —— 切片逻辑坏了，后面的断言会恒真"
    at = helper.index("except Exception")
    except_block = helper[at:]

    assert "return {}" in except_block, "except 分支必须返回空 dict"
    assert not re.search(r"^\s*raise\b", except_block, re.M), \
        "except 分支里不得有 raise —— 取名失败必须 fail-soft，不能阻断下单"
    # 返回必须发生在抛出之前（防止 return 后面又跟 raise 之类的怪写法）
    assert except_block.index("return {}") < len(except_block), "except 分支必须以返回收尾"


def test_writeback_self_heals_empty_name_only():
    """回写分支要自愈空名，但**不能覆盖已有名字**。"""
    hits = re.findall(r"media_name = COALESCE\(NULLIF\(media_name, ''\), %s\)", DB)
    assert len(hits) == 2, f"两个回写分支都要自愈，实际命中 {len(hits)} 处"
    # 反向：不允许无条件覆盖
    assert not re.search(r"SET[^;]*\bmedia_name = %s(?![^;]*COALESCE)", DB), \
        "不得无条件覆盖 media_name —— 会把正确的名字冲掉"


def test_reverse_writeback_still_updates_status():
    """反向锁：加了 media_name 自愈后，原本的 status/url/reject_reason 回写不能丢。"""
    for col in ("status = %s", "publish_url = %s", "reject_reason = %s"):
        assert DB.count(col) >= 2, f"回写分支丢了 {col}"
