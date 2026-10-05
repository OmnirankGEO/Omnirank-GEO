"""
tools/xiaobang_system_kb.py — 系统知识库确定性拆 chunk（Task 2 · 2026-05-29）

从 final 目录（knowledge/system_kb/pages/*.md）读取人审真值，
按 SCHEMA.md § 4 规则拆出 sys_page / sys_field / sys_error chunk，
写入 kb_chunks 表，不调用任何 LLM、不新增事实。

导出:
  parse_final_page(path) -> dict
  reindex_system(pages_glob=...) -> int
"""

import glob
import io
import logging
import os
import re
from typing import Any

import yaml

from db.kb_db import clear_system_chunks, insert_chunk, replace_chunks_transactionally
from tools.xiaobang_kb_indexer import tokenize

logger = logging.getLogger("system-kb")


class SystemKbReleaseAborted(RuntimeError):
    """本次 system page 重建被放弃,**上一 release 完整保留**。

    刻意是异常而不是返回码:返回 0 会被调用方当成"没有页面"而继续往下走,
    而这里的事实是"有页面但我们不敢发"。两件事必须能分开。
    """

# ============================================================
# 内部：解析 final .md 文件
# ============================================================

_SECTION_TITLES = ["用途", "字段", "按钮", "常见异常", "用户常问", "流程", "状态"]

# 全角冒号分隔符（字段行）
_FULLWIDTH_COLON = "："
# 异常行箭头分隔符
_ARROW = "→"


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """分离 YAML frontmatter 和正文。

    返回 (frontmatter_dict, body_text)。
    若无 frontmatter，frontmatter_dict 为空 dict。
    """
    raw = raw.lstrip("﻿")  # 去 BOM
    if not raw.startswith("---"):
        return {}, raw
    # 找第二个 ---
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    fm_text = raw[3:end].strip()
    body = raw[end + 4:].lstrip("\n")
    fm = yaml.safe_load(fm_text) or {}
    return fm, body


def _split_sections(body: str) -> dict[str, str]:
    """按 `## <section>` 分割正文，返回 {section_title: content_text}。"""
    sections: dict[str, str] = {}
    current_title: str | None = None
    lines: list[str] = []

    for line in body.splitlines():
        m = re.match(r"^##\s+(.+)$", line)
        if m:
            if current_title is not None:
                sections[current_title] = "\n".join(lines).strip()
            current_title = m.group(1).strip()
            lines = []
        else:
            if current_title is not None:
                lines.append(line)

    if current_title is not None:
        sections[current_title] = "\n".join(lines).strip()

    return sections


def _parse_field_lines(section_text: str) -> list[dict[str, Any]]:
    """解析 `## 字段` 节，每行格式：`- <name>（必填|可选）：<meaning>`

    全角冒号 `：` 分隔 name 部分和 meaning 部分。
    括注 `（必填）` → required=True，其余 → False。
    行内 `[仅代理]` 标记 → normal_user_can_know=False（该字段属代理专属，
    在共享页里这一条 chunk 只进代理 KB，不进普通用户 KB）。
    """
    fields: list[dict[str, Any]] = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        content = line[2:].strip()
        if _FULLWIDTH_COLON not in content:
            continue
        # 先判 [仅代理] 标记（可出现在行任意位置），再剥离不污染 meaning
        agent_only = "[仅代理]" in content
        content = content.replace("[仅代理]", "").strip()
        left, meaning = content.split(_FULLWIDTH_COLON, 1)
        # 提取括注
        m = re.search(r"（(必填|可选)）", left)
        required = m.group(1) == "必填" if m else False
        # name 是括注前主体
        name = re.sub(r"（(必填|可选)）", "", left).strip()
        fields.append({
            "name": name,
            "required": required,
            "meaning": meaning.strip(),
            "normal_user_can_know": not agent_only,
        })
    return fields


def _parse_button_lines(section_text: str) -> list[dict[str, Any]]:
    """解析 `## 按钮` 节，每行格式：`- <name>：<点击后果/何时点击/禁用报错>`

    全角冒号 `：` 分隔按钮名与含义；无冒号时整行作为按钮名、含义留空。
    行内 `[仅代理]` 标记 → normal_user_can_know=False（共享页里该按钮的 sys_button
    chunk 收紧到代理 KB，不进普通用户 KB）。
    """
    buttons: list[dict[str, Any]] = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        content = line[2:].strip()
        agent_only = "[仅代理]" in content
        content = content.replace("[仅代理]", "").strip()
        if _FULLWIDTH_COLON in content:
            name, meaning = content.split(_FULLWIDTH_COLON, 1)
        else:
            name, meaning = content, ""
        buttons.append({
            "name": name.strip(),
            "meaning": meaning.strip(),
            "normal_user_can_know": not agent_only,
        })
    return buttons


def _parse_qa_lines(section_text: str) -> list[dict[str, Any]]:
    """解析 `## 用户常问` 节，每行格式：`- <问题> → <标准答案>`

    箭头 `→`（U+2192）分隔问题与答案。无箭头时整行作为问题、答案留空（过渡兼容）。
    行内 `[仅代理]` 标记 → normal_user_can_know=False（共享页里该 Q&A 的 sys_qa chunk
    收紧到代理 KB，不进普通用户 KB）。
    """
    qas: list[dict[str, Any]] = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        content = line[2:].strip()
        agent_only = "[仅代理]" in content
        content = content.replace("[仅代理]", "").strip()
        if _ARROW in content:
            q, a = content.split(_ARROW, 1)
        else:
            q, a = content, ""
        qas.append({
            "question": q.strip(),
            "answer": a.strip(),
            "normal_user_can_know": not agent_only,
        })
    return qas


def _parse_error_lines(section_text: str) -> list[dict[str, str]]:
    """解析 `## 常见异常` 节，每行格式：`- <trigger> → <fix>`"""
    errors: list[dict[str, str]] = []
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        content = line[2:].strip()
        if _ARROW not in content:
            continue
        trigger, fix = content.split(_ARROW, 1)
        errors.append({"trigger": trigger.strip(), "fix": fix.strip()})
    return errors


def _parse_list_lines(section_text: str) -> list[str]:
    """解析有序/无序列表行，返回纯文本列表。"""
    items: list[str] = []
    for line in section_text.splitlines():
        line = line.strip()
        # 无序列表 `- xxx` 或有序 `1. xxx`
        m = re.match(r"^(?:-|\d+\.)\s+(.+)$", line)
        if m:
            items.append(m.group(1).strip())
    return items


# ============================================================
# 公开 API
# ============================================================

def parse_final_page(path: str) -> dict[str, Any]:
    """读取一个 final .md 文件，返回结构化 page card dict。

    Args:
        path: 文件路径（相对于当前工作目录或绝对路径均可）。

    Returns:
        dict 含：
          route, page_name, is_admin_only, visible_to,
          purpose, fields, buttons, validation_errors,
          common_questions（[{question, answer, normal_user_can_know}]）,
          screenshot_guide（截图问答指引列表）, step_flow, status_notes
    """
    # 支持相对路径（相对于项目根，即 cwd）
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)

    with open(path, encoding="utf-8") as f:
        raw = f.read()

    fm, body = _split_frontmatter(raw)
    sections = _split_sections(body)

    # visible_to: agent | normal_user | both（缺省 both，向后兼容老 final 页）
    visible_to = str(fm.get("visible_to", "both")).strip() or "both"
    if visible_to not in ("agent", "normal_user", "both"):
        visible_to = "both"

    return {
        "route": fm.get("route", ""),
        "page_name": fm.get("page_name", ""),
        "is_admin_only": bool(fm.get("is_admin_only", False)),
        "visible_to": visible_to,
        "purpose": sections.get("用途", "").strip(),
        "fields": _parse_field_lines(sections.get("字段", "")),
        "buttons": _parse_button_lines(sections.get("按钮", "")),
        "validation_errors": _parse_error_lines(sections.get("常见异常", "")),
        "common_questions": _parse_qa_lines(sections.get("用户常问", "")),
        "screenshot_guide": _parse_list_lines(sections.get("截图问答指引", "")),
        "step_flow": _parse_list_lines(sections.get("流程", "")),
        "status_notes": sections.get("状态", "").strip(),
    }


def _build_chunks(card: dict[str, Any]) -> list[dict[str, Any]]:
    """将 page card 拆成 chunk 列表（不写库，只构建）。"""
    route = card["route"]
    page_name = card["page_name"]
    is_admin_only = card["is_admin_only"]
    page_visible_to = card.get("visible_to", "both")
    chunks: list[dict[str, Any]] = []

    # ---- sys_page chunk ----
    # 注：用户常问改为独立的 sys_qa chunk（带标准答案、可身份隔离），不再并进 sys_page。
    page_parts: list[str] = []
    if card["purpose"]:
        page_parts.append(card["purpose"])
    if card["step_flow"]:
        page_parts.append("流程：" + "；".join(card["step_flow"]))
    if card.get("screenshot_guide"):
        page_parts.append("截图问答指引：" + "；".join(card["screenshot_guide"]))
    if card["status_notes"]:
        page_parts.append(card["status_notes"])
    page_content = "\n".join(page_parts)

    chunks.append({
        "source_type": "sys_page",
        "source_slug": route,
        "source_title": page_name,
        "section_title": "页面",
        "content": page_content,
        "route": route,
        "is_admin_only": is_admin_only,
        "visible_to": page_visible_to,
    })

    # ---- sys_field chunk（每字段一条）----
    for field in card["fields"]:
        req_label = "必填" if field["required"] else "可选"
        content = f"{field['name']}：{field['meaning']}（{req_label}）"
        # 字段级内容闸：共享页（both）里被标 [仅代理] 的字段，这条 chunk 收紧到 agent，
        # 不进普通用户 KB；代理独有/普通用户独有页则随页面 visible_to。
        field_visible_to = page_visible_to
        if page_visible_to == "both" and not field.get("normal_user_can_know", True):
            field_visible_to = "agent"
        chunks.append({
            "source_type": "sys_field",
            "source_slug": f"{route}#{field['name']}",
            "source_title": page_name,
            "section_title": field["name"],
            "content": content,
            "route": route,
            "is_admin_only": is_admin_only,
            "visible_to": field_visible_to,
        })

    # ---- sys_button chunk（每按钮一条）----
    # 截图问答主力：用户拿截图问"这个按钮干嘛/点了会怎样/为什么点不了"。
    # 内容含按钮名 + 点击后果/何时点击/禁用报错（来自 ## 按钮 行的含义文本）。
    for btn in card.get("buttons", []):
        name = btn["name"] if isinstance(btn, dict) else str(btn)
        meaning = btn.get("meaning", "") if isinstance(btn, dict) else ""
        content = f"按钮「{name}」：{meaning}" if meaning else f"按钮「{name}」"
        # 按钮级内容闸：共享页（both）里被标 [仅代理] 的按钮，这条 chunk 收紧到 agent。
        btn_visible_to = page_visible_to
        if page_visible_to == "both" and isinstance(btn, dict) and not btn.get("normal_user_can_know", True):
            btn_visible_to = "agent"
        chunks.append({
            "source_type": "sys_button",
            "source_slug": f"{route}#btn-{name}",
            "source_title": page_name,
            "section_title": name,
            "content": content,
            "route": route,
            "is_admin_only": is_admin_only,
            "visible_to": btn_visible_to,
        })

    # ---- sys_error chunk（每条异常一条）----
    for i, err in enumerate(card["validation_errors"]):
        content = f"{err['trigger']} → {err['fix']}"
        chunks.append({
            "source_type": "sys_error",
            "source_slug": f"{route}#err-{i}",
            "source_title": page_name,
            "section_title": "异常",
            "content": content,
            "route": route,
            "is_admin_only": is_admin_only,
            "visible_to": page_visible_to,
        })

    # ---- sys_qa chunk（每条用户常问一条，含标准答案）----
    # 截图问答直答主力：用户问法匹配到问题 → 直接返回标准答案，减少模型跨段拼答。
    for i, qa in enumerate(card.get("common_questions", [])):
        if isinstance(qa, dict):
            question = qa.get("question", "")
            answer = qa.get("answer", "")
            nuk = qa.get("normal_user_can_know", True)
        else:  # 过渡兼容：老格式纯问题字符串
            question, answer, nuk = str(qa), "", True
        content = f"{question} 答：{answer}" if answer else question
        # Q&A 级内容闸：共享页里被标 [仅代理] 的问答收紧到 agent。
        qa_visible_to = page_visible_to
        if page_visible_to == "both" and not nuk:
            qa_visible_to = "agent"
        chunks.append({
            "source_type": "sys_qa",
            "source_slug": f"{route}#qa-{i}",
            "source_title": page_name,
            "section_title": question[:40] if question else "问答",
            "content": content,
            "route": route,
            "is_admin_only": is_admin_only,
            "visible_to": qa_visible_to,
        })

    return chunks


SYS_SOURCE_TYPES = ["sys_page", "sys_field", "sys_button", "sys_error", "sys_qa"]


def _assert_kb_page_is_geo_only(path: str, card: dict) -> None:
    """把两条 Owner 铁律接到**索引构建链**上(窗G 段二①)。

    在这之前 ``services/defensive_geo/xiaobang/kb_entries.py`` 是一个
    **只有判据在 import 的死函数** —— 也就是说「小榜的知识库里没有社媒条目」
    这句话此前没有任何运行时的东西在守。

    两道方向相反的检查一起用才既不漏也不误伤:

    * :func:`assert_entry_is_geo_only` 判**结构**(这条目在不在教用户用社媒能力),
      复用现役四轴锁 ``xiaobang_command_contract.social_domain_hits``;
    * :func:`assert_no_unregistered_social_mention` 判**措辞**兜底(有没有没人过目
      就混进来的社媒字样),命中且未登记即拒。

    只有结构锁 ⇒ 一段介绍社媒套餐的散文不会被拦;
    只有措辞锁 ⇒ 会去删真话(价目页上确实有「社媒IP」这个 Tab)。
    """
    from services.defensive_geo.xiaobang.kb_entries import (
        KbEntry, assert_entry_is_geo_only, assert_no_unregistered_social_mention,
    )

    slug = os.path.splitext(os.path.basename(path))[0]
    route = str(card.get("route") or "")
    assert_entry_is_geo_only(KbEntry(
        entry_id=slug,
        title=str(card.get("page_name") or ""),
        route=route,
        # 页卡没有 capability 这一栏 —— 传空串而不是编一个:
        # 编出来的 capability 会让四轴里那一轴变成噪声。
        capability="",
        body="",
    ))
    rel = path.replace("\\", "/")
    marker = "knowledge/system_kb/pages/"
    if marker in rel:
        rel = rel[rel.index(marker):]
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        assert_no_unregistered_social_mention(rel, fh.read())


def reindex_system(pages_glob: str = "knowledge/system_kb/pages/*.md",
                   with_embeddings: bool = True) -> int:
    """从 final 目录确定性重建系统知识库 chunk。

    Args:
        pages_glob: glob 模式或单文件路径。下划线开头的样例/草稿页（`_*.md`）
            一律排除，不入库。
        with_embeddings: 入库后是否同步给 sys_* chunk 生成 embedding(DashScope)。
            默认 True;失败仅告警(BM25 仍可用)。**只补 sys_* 类型,不动旧 doc/faq/preset**。
            测试/纯本地可传 False 跳过网络调用。

    Returns:
        插入的 chunk 总数。
    """
    # 🔴 [窗G 段二①] 声明过的新 KB 页必须**真的**落在本次索引的 glob 下。
    #    拦的是本仓记过的假绿形态:页写了、锁扫了、线上答不出来 ——
    #    因为那个文件根本不进索引。分母是**索引构建链**(就是下面这个 glob),
    #    不是一份文件清单。
    #
    #    🔴 必须在 abspath 转换**之前**比:声明的页是仓内相对路径,
    #       转成绝对之后 fnmatch 必不匹配 —— 那会让这道闸变成"永远抛",
    #       比恒绿更糟(整条重建链当场瘫)。
    #    🔴 只在**目录级全量重建**时检查(basename 含通配符)。
    #       reindex_system 的 docstring 逐字写着它接受「glob 模式**或单文件路径**」——
    #       局部重建是开发/判据的口子,「声明页必须进索引」这条只对发布用的
    #       全量重建成立。第一版漏了这个区分,直接打断了
    #       tests/system_kb/test_reindex_system.py:101(它用单文件窄 glob,
    #       期望 SystemKbReleaseAborted,却先吃到 KbEntryError)——
    #       而那个家族本包一次都没跑过。
    if not os.path.isabs(pages_glob) and '*' in os.path.basename(pages_glob):
        from services.defensive_geo.xiaobang.kb_entries import (
            assert_declared_pages_are_indexed,
        )

        assert_declared_pages_are_indexed([pages_glob])

    # 如果是相对路径，拼成绝对路径再 glob
    if not os.path.isabs(pages_glob):
        pages_glob = os.path.join(os.getcwd(), pages_glob)

    files = glob.glob(pages_glob)
    # 排除下划线开头的样例/草稿页（如 _example-pricing.md），它们不入库
    files = [f for f in files if not os.path.basename(f).startswith("_")]
    if not files:
        # 🔴 [R3-P9 ④] 零匹配 **不是**「没事发生」,是 fail-open 角。
        #    原来这里 warning + ``return 0``:调用方拿到 0 与「本来就 0 页」无法区分,
        #    而 warning 在生产里没人盯。真实触发形态是**部署期 cwd 不对 / 页目录没进镜像**
        #    —— 那一刻正确的行为是「什么都别动,保留上一 release」,而不是
        #    「安静地宣布这次重建成功」。③ 把整条链改成 fail-closed 之后,
        #    这一个角是唯一还留着的 fail-open 出口,不留。
        raise SystemKbReleaseAborted(
            "[system-kb] glob {0!r} 匹配到 0 个 final 页(已排除 _*.md)。"
            "没有改动任何东西,上一 release 保持在线。"
            "常见原因:进程 cwd 不是仓库根 / 页目录没进镜像 / 传错了 glob。"
            .format(pages_glob))

    # 🔴 [窗G 段二①] 声明过的新 KB 页必须**真的**落在本次索引的 glob 下。
    #    拦的是本仓记过的假绿形态:页写了、锁扫了、线上答不出来 ——
    #    因为那个文件根本不进索引。分母是**索引构建链**(就是上面这个 glob),
    #    不是一份文件清单。
    # ── [R3-P8 ③] 先全量扫,再单事务切换 ─────────────────────────────
    # 原实现:解析(失败就 `logger.exception` **跳过**)→ clear_system_chunks()
    # → 逐条 insert_chunk(每条一个事务)。两个后果:
    #   ① 一页解析坏掉 ⇒ 悄悄发布一份**缺页**的 release(没人收到通知);
    #   ② 任何一条写入失败(含术语门拒绝)⇒ 旧的已经 clear 掉、新的只写了一半
    #      ⇒ 用户当场问不出答案。
    # 🔴 现在:解析 **fail-closed**(不跳过),术语门在**写库之前**全量过一遍,
    #    两步都过了才进单事务 clear+insert。任一失败 ⇒ 上一 release 完整保留。
    all_chunks: list[dict[str, Any]] = []
    parse_failures: list[str] = []
    for fp in sorted(files):
        try:
            card = parse_final_page(fp)
            # 🔴 垃圾 front-matter **不抛异常**,它静默降级成 route='' / page_name=''
            #    ⇒ 发布一批"没有落点"的 chunk(小榜答得出内容却给不出跳转)。
            #    实测现役 26 页全都有这两项,所以把"缺 route/page_name"升成解析失败
            #    是零误伤的,而且它才是这道闸真正要拦的那类。
            if not str(card.get("route") or "").strip():
                raise ValueError("缺 route(front-matter 没解析出来?)")
            if not str(card.get("page_name") or "").strip():
                raise ValueError("缺 page_name")
            # 🔴 [窗G 段二① · Owner 铁律 XIAOBANG-GEO-ONLY 2026-08-18]
            #    **小榜只负责 GEO 板块 —— 社媒条目不许进索引。**
            #    闸开在这里而不是在写库之后:这一整段是 fail-closed 的
            #    (任何一页出问题 ⇒ 放弃本次重建、上一 release 完整保留),
            #    所以拦在这里的后果是"没发布",不是"发布了一半"。
            #    判的是**结构**(路由前缀 / capability 命名空间 / 词干),不判措辞 ——
            #    判措辞会逼下一个人去删一句真话(见 kb_entries 模块 docstring 里
            #    feature-pricing.md 那个反例)。
            _assert_kb_page_is_geo_only(fp, card)
            page_chunks = _build_chunks(card)
        except Exception as exc:
            # 🔴 不再 `continue`:跳过一页 = 发布一份缺页 release,而且是静默的。
            parse_failures.append("{0}: {1}".format(os.path.basename(fp), exc))
            continue
        all_chunks.extend(page_chunks)
        logger.info("[system-kb] 解析 %s → %d chunks", os.path.basename(fp), len(page_chunks))
    if parse_failures:
        raise SystemKbReleaseAborted(
            "解析失败 {0} 页,已放弃本次重建(上一 release 保持 active):{1}".format(
                len(parse_failures), "; ".join(parse_failures[:5])))
    if not all_chunks:
        raise SystemKbReleaseAborted(
            "解析出 0 条 chunk,已放弃本次重建 —— 空 release 会让用户什么都问不出来")

    rows = [{
        "source_type": chunk["source_type"],
        "source_slug": chunk["source_slug"],
        "source_title": chunk["source_title"],
        "section_title": chunk.get("section_title"),
        "content": chunk["content"],
        "route": chunk["route"],
        "route_label": None,
        "category": "system",
        "is_admin_only": chunk["is_admin_only"],
        "token_keywords": tokenize(chunk["content"]),
        "origin": "manual",
        "visible_to": chunk.get("visible_to", "both"),
    } for chunk in all_chunks]

    # 单事务:DELETE 整类 + INSERT 全量。术语门在 DELETE 之前扫完(见 db/kb_db.py)。
    result = replace_chunks_transactionally(
        source_types=tuple(SYS_SOURCE_TYPES), chunks=rows)
    inserted = int(result["inserted"])
    logger.info("[system-kb] 单事务切换完成:淘汰 %d 条,写入 %d 条",
                int(result["deleted"]), inserted)
    logger.info("[system-kb] 写入完成，共 %d chunks", inserted)

    # 入库后同步给系统 chunk 生成 embedding(只补 sys_*,不碰 doc/faq/preset)
    if with_embeddings and inserted > 0:
        try:
            import asyncio
            from tools.xiaobang_kb_indexer import backfill_embeddings
            r = asyncio.run(backfill_embeddings(source_types=SYS_SOURCE_TYPES))
            logger.info("[system-kb] embedding 生成结果: %s", r)
        except Exception:
            logger.exception("[system-kb] embedding 生成失败(跳过,BM25 仍可用)")

    return inserted
