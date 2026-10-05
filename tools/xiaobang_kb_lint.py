"""系统知识库 final 页安全 lint。

用途:
- 按真实解析器拆 chunk,检查普通用户池是否混入代理/管理/内部实现内容。
- 检查容易被解析器漏掉的伪标记,例如标题写 [仅代理] 但子项没有逐行标记。

该工具只读 markdown,不入库、不调用外部模型。
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass
from typing import Iterable

from tools.xiaobang_system_kb import _build_chunks, parse_final_page


NORMAL_POOL_BLOCKLIST = (
    "佣金", "提现", "出厂价", "利润", "库存", "白标", "客户归属",
    "报价系数", "报价加价倍率", "服务费", "收益结算", "代理等级", "直接推荐",
    "管理员账号", "后台价目表", "内部成本", "数据库", "API", "接口",
    "1元=130", "约¥", "约合人民币", "换算",
)

INTERNAL_CODES = (
    "brand_fill", "autofill_brand", "topic_gen", "article_gen", "article_rewrite",
    "deep_analyze", "profile_polish", "geo_diagnosis", "media_proxy_publish",
)

MISLEADING_AGENT_MARKERS = (
    "[仅代理]字段", "[仅代理]按钮", "（必填|代理）", "（可选|代理）",
    "(必填|代理)", "(可选|代理)",
)


# 代理池 L1 越权词:这些是 L2 专属 / 管理员 / 内部维护功能。解析器只有 [仅代理] 一档,
# 写进代理 KB 会被普通代理(L1)的小榜检索到。必须删除或改成"无入口即无权限"的中性口径。
AGENT_POOL_BLOCKLIST = (
    "L2", "普通代理看不到", "内部维护", "管理员功能",
    "同步数据", "AI占比", "AI 占比", "上传调研",
    "权重配置", "渠道服务费", "服务费分账",
)

# agent-agreement 是代理协议正文,合法包含追索/服务费/L2 分层等条款,豁免代理池越权扫描。
AGENT_POOL_EXEMPT_SLUGS = ("agent-agreement",)


@dataclass(frozen=True)
class KBLintIssue:
    file: str
    route: str
    check: str
    detail: str


def _page_files(pages_glob: str) -> list[str]:
    files = glob.glob(pages_glob)
    return sorted(f for f in files if not os.path.basename(f).startswith("_"))


def _is_normal_visible(chunk: dict) -> bool:
    return chunk.get("visible_to") in ("both", "normal_user")


def _is_agent_visible(chunk: dict) -> bool:
    return chunk.get("visible_to") in ("both", "agent")


def lint_pages(pages_glob: str = "knowledge/system_kb/pages/*.md") -> list[KBLintIssue]:
    if not os.path.isabs(pages_glob):
        pages_glob = os.path.join(os.getcwd(), pages_glob)

    issues: list[KBLintIssue] = []
    for path in _page_files(pages_glob):
        card = parse_final_page(path)
        route = card.get("route", "")
        rel = os.path.relpath(path, os.getcwd())
        raw = open(path, encoding="utf-8").read()

        for marker in MISLEADING_AGENT_MARKERS:
            if marker in raw:
                issues.append(KBLintIssue(
                    file=rel,
                    route=route,
                    check="misleading_agent_marker",
                    detail=f"发现 {marker}。代理专属必须逐条写在线内 [仅代理],否则解析器不会收紧。",
                ))

        for code in INTERNAL_CODES:
            if re.search(rf"\b{re.escape(code)}\b", raw):
                issues.append(KBLintIssue(
                    file=rel,
                    route=route,
                    check="internal_code",
                    detail=f"用户知识库不能出现内部功能码 {code}",
                ))

        slug = os.path.splitext(os.path.basename(path))[0]
        for chunk in _build_chunks(card):
            content = str(chunk.get("content") or "")
            if _is_agent_visible(chunk) and slug not in AGENT_POOL_EXEMPT_SLUGS:
                for term in AGENT_POOL_BLOCKLIST:
                    if term in content:
                        issues.append(KBLintIssue(
                            file=rel,
                            route=route,
                            check="agent_pool_l1_overreach",
                            detail=f"代理可见 chunk 含 L1 越权词「{term}」(L2/管理员/内部专属不能进代理 KB,需删除或改中性口径): {content[:120]}",
                        ))
                        break
            if not _is_normal_visible(chunk):
                continue
            for term in NORMAL_POOL_BLOCKLIST:
                # 普通用户推荐返利规则里可以说"不能提现"，这是限制说明，不是代理提现知识。
                if term == "提现" and "不能提现" in content:
                    continue
                if term in content:
                    issues.append(KBLintIssue(
                        file=rel,
                        route=route,
                        check="normal_user_leak",
                        detail=f"普通用户可见 chunk 含「{term}」: {content[:120]}",
                    ))
                    break

    return issues


def format_issues(issues: Iterable[KBLintIssue]) -> str:
    rows = list(issues)
    if not rows:
        return "OK: 系统知识库 lint 未发现问题"
    lines = [f"发现 {len(rows)} 个系统知识库 lint 问题:"]
    for issue in rows:
        lines.append(f"- [{issue.check}] {issue.file} {issue.route}: {issue.detail}")
    return "\n".join(lines)


def main() -> int:
    issues = lint_pages()
    print(format_issues(issues))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
