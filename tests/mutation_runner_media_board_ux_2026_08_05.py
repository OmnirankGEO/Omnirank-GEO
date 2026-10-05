"""变异验证:证明 tests/test_media_board_ux_closure_2026_08_05.py 的每把锁都有判别力。

用法:`python tests/mutation_runner_media_board_ux_2026_08_05.py`

规则(都是本仓踩出来的):
  · **基线必须先绿**。基线就红 = 判据比要挡的行为宽,变异结果无意义。
  · **命中次数 ≠ 1 一律 SKIP,不许标 SURVIVED** —— 没改成的变异当「没杀掉」是自造误报
    (2026-08-04 这条救过两次场)。
  · **变换必须全成或全不成**:任何一步失败立即回滚该轮,不留半成品。
  · 🔴 **读写一律 `newline=""`**(Windows 实测坑):`Path.write_text` 默认按平台改行尾,
    回滚时会把 10 个源文件整份从 LF 写成 CRLF —— 用例照样全绿、`git diff --stat` 却整片飘红。
    行尾问题唯一可信的判据是**字节计数**(`data.count(b"
")`),不是肉眼也不是 diff 观感。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = "tests/test_media_board_ux_closure_2026_08_05.py"

BOARD = "services/media_effectiveness_board.py"
DIRECTORY = "services/media_domain_directory.py"
MANIFEST = "db/migration_manifest.py"
SERVER = "server.py"
CONTRACT = "services/organization_route_contract.py"
REPORT = "services/research_monitor/round_report.py"
SELFSERVE_API = "api/research_selfserve_api.py"
SELFSERVE_DB = "db/research_selfserve_db.py"
PUBLISH_CENTER = "frontend/src/pages/Publishing/PublishCenter.tsx"
DRAWER = "frontend/src/components/publishing/ArticleAdvisoryDrawer.tsx"

# (编号, 说明, 文件, 原文, 替换[, 预期锚点命中次数=1])
# 🔴 预期命中次数写死是刻意的:命中数与预期不符 = 变异没按我以为的方式落地,
#    一律 SKIP 而不是 SURVIVED(把"没改成"当"没杀掉"是自造误报)。
MUTATIONS: list[tuple] = [
    (
        "M1", "§1 目录覆盖越界:任何展示名都当成裸域名 → 平台公认名被蒸馏名盖掉",
        BOARD,
        '    return (not text) or (bool(dom) and text == dom)',
        '    return True',
    ),
    (
        "M2", "§1 接线断开:出榜不再 join 域名目录(能力在、功能等于不存在)",
        BOARD,
        '    _apply_domain_directory(rows, general_rows)',
        '    pass  # mutated: 接线拆掉',
    ),
    (
        "M3", "§1 fail-soft 拆掉:目录查询异常直接往上抛,榜会崩",
        BOARD,
        '''    except Exception as exc:
        logger.warning("[publish-board] 域名目录查询失败(退化为域名展示): %s", str(exc)[:200])
        return''',
        '''    except Exception:
        raise''',
    ),
    (
        "M4", "§1 one_liner 不挂了(hover 拿不到简介)",
        BOARD,
        '        if hit and hit.get("one_liner"):\n            r["one_liner"] = hit["one_liner"]',
        '        pass  # mutated: 不挂 one_liner',
    ),
    (
        "M5", "§1 蒸馏收下模型自行发挥补出来的域名(目录混进臆造条目)",
        DIRECTORY,
        '        if not dom or dom not in allowed:\n            continue',
        '        if not dom:\n            continue',
    ),
    (
        "M6", "§1 upsert 不再保护人工订正行(admin 名会被蒸馏覆盖)",
        DIRECTORY,
        "                 WHERE media_domain_directory.source <> 'admin'",
        "                 -- mutated: 人工订正保护拆掉",
    ),
    (
        "M7", "§1 迁移没登记进 manifest(表永远不会建)",
        MANIFEST,
        '    "db/migration_026_media_domain_directory_2026_08_05.sql",',
        '    # mutated: 忘了登记',
    ),
    (
        "M8", "§2 计数改自己算(不再与列表徽章同源 → 显示 N 条点开 M 条)",
        SERVER,
        '    advisory_state, advisory_open_count = _advisory_state(quality_warning)',
        '    advisory_state, advisory_open_count = "open", 0  # mutated: 第二份口径',
    ),
    (
        "M9", "§2 语义红线破了:明细里说成「审核通过」",
        SERVER,
        '        "advisory_state": advisory_state,\n        "advisory_open_count": advisory_open_count,',
        '        "advisory_state": advisory_state,\n        "badge_text": "审核通过",\n        "advisory_open_count": advisory_open_count,',
    ),
    (
        "M10", "§2 员工席位路由契约没登记(抽屉 fail-closed 打不开)",
        CONTRACT,
        '    _p("GET", "/api/articles/{article_id}/advisory", "writing.read_own", "article", "detail"),',
        '    # mutated: 契约没登记',
    ),
    (
        "M11", "§2 徽章没接线(组件在、点了还是没反应)",
        PUBLISH_CENTER,
        'onOpenAdvisory={openAdvisoryFor}',
        '',
        2,   # 两处文章列表(未分发 / 已分发分组)各接了一次
    ),
    (
        "M12", "§2 忽略动作另造一套(不走 topics 落库 = 不留痕)",
        DRAWER,
        "'/api/articles/batch-advisory-continue'",
        "'/api/articles/ignore-advisory-local'",
    ),
    (
        "M13", "§3 编造分值变化(声称可得)",
        REPORT,
        '        "score_delta_available": False,\n        "score_delta_reason": SCORE_DELTA_REASON,\n        # has_data',
        '        "score_delta_available": True,\n        "score_delta_reason": SCORE_DELTA_REASON,\n        # has_data',
    ),
    (
        "M14", "§3 新进榜判定失真:本轮所有域名都标「新出现」",
        REPORT,
        '                new_domains = [d for d in domains if d not in seen_before]',
        '                new_domains = list(domains)',
    ),
    (
        "M15", "§3 报表 fail-soft 拆掉(取数失败把主流程带崩)",
        REPORT,
        '''    except Exception as exc:
        logger.warning("[round-report] 报表取数失败(返回空报表): %s", str(exc)[:200])
        return empty''',
        '''    except Exception:
        raise''',
    ),
    (
        "M16", "§3 报表端点丢掉属主校验(round_id 当凭证 = 跨租户可读)",
        SELFSERVE_API,
        '    if not task or (not agent["is_admin"] and int(task.get("user_id") or 0) != agent["user_id"]):',
        '    if not task:',
    ),
    (
        "M17", "§3 历轮列表只按行业查(别人花的钱出现在我的列表里)",
        SELFSERVE_DB,
        '             WHERE user_id = %s AND industry_key = %s AND COALESCE(round_id, \'\') <> \'\'',
        '             WHERE industry_key = %s\n               AND COALESCE(round_id, \'\') <> \'\'',
    ),
    (
        "M18", "§3 完成 toast 不再能点进报表(toast 又变回终点)",
        PUBLISH_CENTER,
        'setRoundReportId(rid); setRoundReportOpen(true);',
        '/* mutated: 点了什么也不做 */',
    ),
    (
        "M19", "§1 点击死路的显式空态被换回通用文案",
        PUBLISH_CENTER,
        '「{origin.term}」在媒体库里暂无对应可购渠道',
        '无匹配媒体',
    ),
]


def run_suite() -> bool:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "--no-header", "-x"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return proc.returncode == 0


def main() -> int:
    print("=" * 72)
    print("基线自检(必须绿;基线就红 = 判据比要挡的行为宽,变异结果无意义)")
    print("=" * 72)
    if not run_suite():
        print("!! 基线红 —— 停,先修判据。")
        return 2
    print("基线 GREEN\n")

    killed, survived, skipped = [], [], []
    for entry in MUTATIONS:
        mid, desc, rel, old, new = entry[:5]
        expect_hits = entry[5] if len(entry) > 5 else 1
        path = ROOT / rel
        original = path.read_text(encoding="utf-8", newline="")
        hits = original.count(old)
        if hits != expect_hits:
            # 🔴 命中 != 1 一律 SKIP,**不许标 SURVIVED** —— 没改成的变异当「没杀掉」是自造误报。
            skipped.append((mid, desc, f"锚点命中 {hits} 次(预期 {expect_hits} 次)"))
            print(f"[SKIP    ] {mid} {desc} · 锚点命中 {hits} 次(预期 {expect_hits})")
            continue
        try:
            path.write_text(original.replace(old, new, expect_hits), encoding="utf-8", newline="")
            green = run_suite()
        finally:
            path.write_text(original, encoding="utf-8", newline="")  # 全成或全不成:必回滚(newline="" 见文件头)
        if green:
            survived.append((mid, desc))
            print(f"[SURVIVED] {mid} {desc}  <-- 这把锁抓不住它")
        else:
            killed.append((mid, desc))
            print(f"[KILLED  ] {mid} {desc}")

    print("\n" + "=" * 72)
    print(f"KILLED={len(killed)}  SURVIVED={len(survived)}  SKIPPED={len(skipped)}  / 共 {len(MUTATIONS)}")
    for mid, desc in survived:
        print(f"  SURVIVED {mid}: {desc}")
    for mid, desc, why in skipped:
        print(f"  SKIPPED  {mid}: {desc} ({why})")
    print("=" * 72)

    # 回滚后再跑一次:证明 runner 没把仓库改脏(半成品是最难查的那种)。
    if not run_suite():
        print("!! 回滚后基线红 —— runner 留下了半成品,必须人工检查。")
        return 3
    print("回滚后基线仍 GREEN")
    return 0 if not survived and not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
