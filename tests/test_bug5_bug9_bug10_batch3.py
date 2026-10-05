"""[批 3 · 2026-07-27] BUG-5 / BUG-9 / BUG-10 判别测试

- BUG-5：T5「发布 → 被引」转化率前端零消费 —— 后端早就返回 publish_to_citation，
  前端全仓 grep 零命中，工单 T5「垂类去留用数据自证」当前做不到（没人看得见这个数）。
- BUG-9：articles.brand_id 全表 NULL，却仍有消费方在拿它算品牌文章数。
- BUG-10：飞轮三张表只有懒初始化，没挂启动链，建表失败还被外层 except 吞掉。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend" / "src"


# ============================================================
# BUG-5 · T5 转化率前端渲染
# ============================================================

PANEL = FE / "components" / "publishing" / "MediaEffectivenessPanel.tsx"


def test_t5_conversion_is_consumed_by_frontend():
    """后端返回的 publish_to_citation 必须真被解包渲染，不能再零消费。"""
    src = PANEL.read_text(encoding="utf-8")
    assert "publish_to_citation" in src, "T5 数据未被前端解包"
    assert "setConversion(data?.publish_to_citation" in src, "必须从同一个 response 解包"
    assert "发布 → 被引" in src, "必须有用户看得见的标题"


def test_t5_adds_no_new_request():
    """组件本来就 fetch 了整个 response —— T5 只准解包，不准新增请求。"""
    src = PANEL.read_text(encoding="utf-8")
    fetches = re.findall(r"authFetch\(", src)
    assert len(fetches) == 1, f"该组件应只有 1 个请求，实际 {len(fetches)} 个"


def test_t5_fail_soft_no_placeholder():
    """遵循该组件既有 fail-soft 规则：无数据不渲染该段，不占位、不报错。"""
    src = PANEL.read_text(encoding="utf-8")
    assert "conversionRows.length > 0 && (" in src, "无数据时整段不得渲染"
    # 降级数据不得当真数展示
    assert "degraded_reason" in src, "后端聚合降级时不得展示假数"
    # 错误/切行业时要清帧，不能把上一个行业的转化率挂到新标题下
    assert src.count("setConversion(null)") >= 2, "早退与失败路径都要清帧"


def test_t5_dark_mode_paired():
    """暗色必须成对：新加的颜色要么是语义 token，要么有 dark: 变体。"""
    src = PANEL.read_text(encoding="utf-8")
    start = src.index("发布 → 被引")
    block = src[start:src.index("按我们实际发出去的那条链接统计", start)]
    # 每个亮色 emerald 后面必须紧跟 dark: 变体（成对写法）
    fixed = re.findall(r"text-emerald-\d00(?!\S)", block)
    paired = re.findall(r"text-emerald-\d00 dark:text-emerald-\d00", block)
    assert len(fixed) == len(paired) * 2, \
        f"固定色未成对配暗色变体: 共 {len(fixed)} 处，成对 {len(paired)} 组"
    assert paired, "T5 段应至少有一处成对写法"
    # 其余一律走语义 token
    assert "text-muted-foreground" in block and "text-foreground" in block


# ============================================================
# BUG-9 · articles.brand_id 全表 NULL
# ============================================================

def test_brand_article_count_does_not_read_null_column():
    """生产实证 articles 1199 行 brand_id 全 NULL；品牌归属必须走 quote_id → quotes.brand_id。"""
    src = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")
    assert "FROM articles WHERE brand_id=" not in src, \
        "不得再从恒 NULL 的 articles.brand_id 取品牌归属"
    assert "FROM articles a JOIN quotes q ON q.id = a.quote_id" in src


def test_brand_id_null_hazard_is_documented():
    """口径必须钉在代码里，否则下一个人还会去读这列。"""
    src = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")
    idx = src.index("FROM articles a JOIN quotes q ON q.id = a.quote_id")
    note = src[max(0, idx - 900):idx]
    assert "BUG-9" in note and "NULL" in note


# ============================================================
# BUG-10 · 飞轮建表挂启动自检 + fail-loud
# ============================================================

SERVER_SRC = (ROOT / "server.py").read_text(encoding="utf-8")


def _startup_selfcheck_body() -> str:
    """取自检函数体（def 行到下一个顶层语句为止）。"""
    start = SERVER_SRC.index("def _init_flywheel_late_tables(")
    rest = SERVER_SRC[start:]
    lines = rest.splitlines(keepends=True)
    out = [lines[0]]
    for line in lines[1:]:
        if line.strip() and not line[0].isspace():
            break
        out.append(line)
    return "".join(out)


def test_flywheel_tables_wired_into_startup():
    """必须有【顶层调用】—— 只定义不调用等于没挂启动链。"""
    assert re.search(r"^_init_flywheel_late_tables\(\)\s*$", SERVER_SRC, re.M), \
        "自检函数未在模块顶层被调用，仍然只是懒初始化"
    body = _startup_selfcheck_body()
    for fn in ("init_flywheel_bridge_tables",
               "init_flywheel_corpus_label_tables",
               "init_flywheel_heartbeat_tables"):
        assert fn in body, f"{fn} 未纳入启动自检"


def test_flywheel_startup_is_fail_loud():
    """建表失败必须 error 级别喊出来 —— 原来是外层 except 吞掉，零告警。"""
    body = _startup_selfcheck_body()
    assert "logger.error(" in body, "失败必须 fail-loud"
    assert "failures.append" in body, "每张表独立捕获，一张失败不掩盖其余"
    assert re.search(r"logger\.warning\([^)]*failures", body) is None


def test_flywheel_startup_bypasses_process_cache():
    """两个 init 有 _TABLE_READY 进程缓存；启动自检必须 force=True 真跑一次 DDL。"""
    assert _startup_selfcheck_body().count('"force": True') == 2, \
        "带 _TABLE_READY 缓存的两个 init 必须 force=True，否则自检可能被跳过"


def test_flywheel_startup_does_not_block_boot():
    """飞轮是旁路能力，不该让主链路起不来 —— fail-loud 但不 raise。"""
    assert "raise" not in _startup_selfcheck_body()
