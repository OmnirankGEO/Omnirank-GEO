"""粗体伪标题 → 真实 Markdown `##` 的**局部**格式修复器。

## 为什么需要它(W1 返工 · 2026-08-08)

LLM 有相当概率用独占一行的 `**粗体**` 冒充小标题。这不是排版偏好问题,是**功能性失效**:

| 下游 | 依赖 | 伪标题下的结果 |
|---|---|---|
| `article_writer.validate_article_structure` H4 | `H2_PATTERN` = `^##\\s+` | 0 个 → hard fail |
| `article_writer.count_effective_answer_blocks` | `HEADING_PATTERN` 切块 | 块数 0 → S5 软失败、H6 连带 |
| `services.article_review_autopilot` | 修的是证据类 hard | **修不了格式**,残留 hard 进草稿态 |

生产实测(20 班本地预演,同客户同批次):历史批 0/10、新批 4/10。
**4/10 而不是 10/10** —— 这是间歇性漂移,说明提示词那一道"劝"不彻底,
所以必须有这第二道机械转换。反过来也成立:只有修复器而不改提示词,
等于让模型继续产劣稿、后面每一环都在给它擦屁股。两道都要。

## 为什么叫「局部」

**只在这篇文章确实缺小标题时才动手**,判据直接用 H4 的那个门槛
(`article_writer.MIN_H2_HEADINGS`,import 复用而不是再写一个 6)。
一篇已经有足量真 `##` 的文章里,`**粗体**` 就是正常的强调,碰它是纯破坏。

其余保守约束(宁可漏修,不可错改):

* 只认**整行**都是粗体的行 —— 行内强调、`- **项**:说明` 这类列表项一概不碰;
* 冒号结尾的(`**结论:**`)是引导语不是标题,不碰;
* 句末标点结尾的、超长的(像句子而不像标题的)不碰;
* 代码围栏内、表格行内不碰;
* 文档第一个非空行不碰 —— 那是 H1 标题的领地,归
  `article_generator_service._normalize_article_title_and_h1` 管,
  两个函数抢同一行会写出重复标题。

修复结果整条留痕返回,进 `article['quality_warning']`,不做静默改写。
"""
from __future__ import annotations

import re
from typing import Final

from .article_writer import H2_PATTERN, MIN_H2_HEADINGS

#: 版本号随规则改动递增(留痕里带上,便于事后按版本回溯改了哪一批)。
HEADING_REPAIR_VERSION: Final = "md-heading-repair-v1"

#: 整行都是粗体:`**文字**`,前后允许空白,**粗体之外不许有任何字符**。
#: 这条是"局部"的第一道闸 —— `- **项**:说明` / `**强调**在句中` 都匹配不上。
_WHOLE_LINE_BOLD: Final = re.compile(r'^\s*\*\*(?P<text>[^*].*?)\*\*\s*$')
#: 代码围栏(``` 或 ~~~),成对切换。
_FENCE: Final = re.compile(r'^\s*(?:```|~~~)')
#: 标题文字的长度上限。超过就更像被整行加粗的句子,不是标题。
MAX_HEADING_CHARS: Final = 40
#: 这些结尾说明它是句子或引导语,不是标题。
_NON_HEADING_TAIL: Final = ("。", "!", "?", "!", "?", ";", ";", ":", ":", ",", ",")


def _looks_like_heading(text: str) -> bool:
    """粗体里的内容像不像一个小标题。"""
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > MAX_HEADING_CHARS:
        return False
    if stripped.endswith(_NON_HEADING_TAIL):
        return False
    # 里面还有 `*` 说明嵌套/未闭合,形态不确定,不动。
    if "*" in stripped:
        return False
    return True


def count_real_h2(content: str) -> int:
    """真实 `## ` 小标题的个数。**与 H4 用的是同一个正则**。"""
    return len(H2_PATTERN.findall(content or ""))


#: 三级标题行。用来处理「整篇只有 `###` 没有 `##`」那一种。
_H3_LINE: Final = re.compile(r'^###(?=\s)', re.MULTILINE)


def promote_orphan_h3(content: str) -> tuple[str, int]:
    """整篇**一个 `##` 都没有、却有一整层 `###`** 时,把那一层提到 `##`。

    这一条不是工单要求的,是**真跑实证逼出来的**:两臂对照跑里,不带格式要求的那一臂
    并没有写粗体伪标题,它写的是规规矩矩的 `### 一、…` —— 而 H4 数的是 `^##\\s+`,
    `###` 后面跟的是 `#` 不是空白,**一条都数不到**。也就是说"伪标题"只是 H4 失败的
    其中一种形态,`###` 打头是另一种,修了前者不修后者等于修了一半。

    闸开得很窄,避免把正常的层级压平:
    * 正文里(不含首行标题)**必须一个 `##` 都没有** —— 只要有混合层级就完全不动;
    * `###` 的条数必须够得上 `MIN_H2_HEADINGS`,否则它可能只是几个小注解。
    """
    text = content or ""
    if not text:
        return content, 0
    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return content, 0
    body = "\n".join(lines[first + 1:])
    if count_real_h2(body) > 0:
        return content, 0  # 有混合层级,不碰
    h3_count = len(_H3_LINE.findall(body))
    if h3_count < MIN_H2_HEADINGS:
        return content, 0
    promoted = "\n".join(
        lines[: first + 1] + [_H3_LINE.sub("##", ln, count=1) for ln in lines[first + 1:]]
    )
    if text.endswith("\n"):
        promoted += "\n"
    return promoted, h3_count


def repair_pseudo_headings(content: str) -> tuple[str, list[str]]:
    """把独占一行的粗体伪标题转成 `## `。

    Returns:
        ``(修复后正文, 被转换的标题文字列表)``。没动过任何一行时,
        返回的正文是**原对象**,列表为空 —— 调用方可以据此判断"有没有真的修"。
    """
    text = content or ""
    if not text:
        return content, []
    # 局部闸:已经有足量真小标题的文章不碰。
    if count_real_h2(text) >= MIN_H2_HEADINGS:
        return content, []

    lines = text.splitlines()
    in_fence = False
    seen_first_nonblank = False
    repaired: list[str] = []
    out: list[str] = list(lines)

    for idx, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            seen_first_nonblank = True
            continue
        if in_fence:
            continue
        if not line.strip():
            continue
        if not seen_first_nonblank:
            # 文档第一个非空行 = H1 领地,交给标题归一化,这里一律不碰。
            seen_first_nonblank = True
            continue
        if line.lstrip().startswith("|"):
            continue  # 表格行
        m = _WHOLE_LINE_BOLD.match(line)
        if not m:
            continue
        inner = m.group("text").strip()
        if not _looks_like_heading(inner):
            continue
        out[idx] = f"## {inner}"
        repaired.append(inner)

    if not repaired:
        return content, []
    joined = "\n".join(out)
    if text.endswith("\n"):
        joined += "\n"
    return joined, repaired


def heading_repair_note(repaired: list[str]) -> dict:
    """给 `article['quality_warning']` 用的留痕结构(不做静默改写)。"""
    return {
        "version": HEADING_REPAIR_VERSION,
        "converted_count": len(repaired),
        "converted_titles": list(repaired),
    }


def repair_article_for_save(article: dict) -> dict:
    """保存链入口:就地修 `article["content"]`,留痕并进 `quality_warning`。

    调用位置与 `body_internal_marker_sanitizer.sanitize_article_for_save` **同一处**
    —— 必须在 `build_article_lineage` **之前**,否则 `current_content_hash` 覆盖的是
    修复前正文,与入库正文不一致 = 对象身份漂移(H0)。

    D8 零阻断:**永不抛异常、永不丢稿**。任何异常都吞掉并原样返回 ——
    宁可正文里留着粗体伪标题,也不能因为修不动就让文章存不下来。
    """
    if not isinstance(article, dict):
        return article
    try:
        original = article.get("content")
        if not isinstance(original, str) or not original.strip():
            return article
        repaired_text, converted = repair_pseudo_headings(original)
        repaired_text, promoted = promote_orphan_h3(repaired_text)
        if not converted and not promoted:
            return article
        if not repaired_text.strip():
            return article
        article["content"] = repaired_text
        warning = article.get("quality_warning")
        if not isinstance(warning, dict):
            warning = {"legacy": warning} if warning else {}
        note = heading_repair_note(converted)
        if promoted:
            note["promoted_h3_count"] = promoted
        warning["heading_repair"] = note
        article["quality_warning"] = warning
    except Exception:  # noqa: BLE001 - 格式修复失败绝不能阻断保存
        pass
    return article
