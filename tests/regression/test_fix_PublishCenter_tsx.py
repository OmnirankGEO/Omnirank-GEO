"""Source-inspection regression locks for PublishCenter.tsx GEO fixes.

These are discriminative source-text assertions: reverting any of the four fixes
makes the corresponding assertion fail. No DB / no server import required
(the target is a frontend .tsx file).

Covered findings:
  - GEO-GREEN-CAN-001: recovery effect clears stale cross-industry active task
  - GEO-GREEN-CAN-002: poll retry-budget exhaustion no longer silently dies
  - GEO-R2-CAN-027: no title-as-body fallback; require non-empty content
    —— [WO_273 · 2026-09-23] 肯定式退役:被测的自助发布路径已删,见文件末段
  - GEO-R2-CAN-028: timeout no longer optimistically persists 'success'
    —— [WO_273 · 2026-09-23] 肯定式退役:同上
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TSX = ROOT / "frontend" / "src" / "pages" / "Publishing" / "PublishCenter.tsx"


def _src() -> str:
    return TSX.read_text(encoding="utf-8")


def test_target_file_exists():
    assert TSX.exists(), f"target file missing: {TSX}"


# ---------------------------------------------------------------------------
# GEO-GREEN-CAN-001 — stale cross-industry active-task must be cleared
# ---------------------------------------------------------------------------
def test_can001_clears_stale_industry_task():
    src = _src()
    assert "[GEO-GREEN-CAN-001]" in src, "CAN-001 fix marker missing"
    # active_task:null branch must clear a task whose industryKey differs.
    assert "prev.industryKey !== ik ? null : prev" in src, (
        "CAN-001: expected clear-on-mismatched-industryKey logic in recovery effect"
    )


# ---------------------------------------------------------------------------
# GEO-GREEN-CAN-002 — poll error-budget exhaustion must not silently die
# ---------------------------------------------------------------------------
def test_can002_poll_exhaustion_backs_off_instead_of_dead():
    src = _src()
    assert "[GEO-GREEN-CAN-002]" in src, "CAN-002 fix marker missing"
    # The old dead-poller one-liner `{ stopped = true; return; }` inside onErr
    # must be gone; a low-frequency backoff reschedule must take its place.
    assert "if (consecutiveErrors >= SELFRES_MAX_POLL_ERRORS) { stopped = true; return; }" not in src, (
        "CAN-002: silent dead-poller (stopped=true) still present at retry exhaustion"
    )
    onerr = src[src.index("const onErr = ()"):src.index("const poll = async ()")]
    assert "SELFRES_POLL_OVERRUN_INTERVAL_MS" in onerr and "consecutiveErrors = 0" in onerr, (
        "CAN-002: onErr must reset the counter and reschedule a low-frequency backoff"
    )


# ---------------------------------------------------------------------------
# GEO-R2-CAN-027 / GEO-R2-CAN-028 —— [WO_273 · 2026-09-23 肯定式退役]
# ---------------------------------------------------------------------------
# 原来三格:
#   · `test_can027_no_title_as_body_fallback`:自助发布 `handleCreateDraft` 取文章详情失败时,
#     不许拿标题当正文发出去(空正文要 continue 跳过、留可重试的失败态);
#   · `test_can028_timeout_does_not_fake_success`:插件回执轮询的兜底超时,不许乐观写 success;
#   · `test_can028_success_still_persisted_from_confirmed_poll`:success 只许由确认过的轮询结果
#     (`pollForResult` → `/api/extension/publish-result`)写入。(这一格在 WO_273 之前就是红的,
#     与本单无关;被测对象同在自助路径,一并退役 —— 否则本文件会剩一格永远红的死锁。)
# 被测对象**整条删了**:WO_273-A(21afc6084)随浏览器插件自助发布退役,删掉 PublishCenter 的
# 自助模式全部分支 —— `handleCreateDraft`(自助版)/ `pollForResult` / 兜底超时块 /
# 两个修复标记在 frontend/src 里都已是 0 处(全仓剩下的那个同名 `handleCreateDraft`
# 在 Admin/PricingCenterSSOT.tsx,是定价页,与发布无关)。代发路径走后端订单,不经过这段代码。
# 接替(「它不许回来」):build 链第 69 步 `frontend/scripts/test-self-publish-retired.mjs`
#   S1 PublishCenter 自助模式键 = 0 行;S2 frontend/src 全部文件 `/api/extension` = 0 行
#   (轮询端点 publish-result 与发布端点都在其中);每次 bake 跑。
# 登记:tests/RETIRED_TESTS.txt 按格各一行。
