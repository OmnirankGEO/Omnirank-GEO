# -*- coding: utf-8 -*-
"""把分类表渲染成签入文档。

🔴 文档由**数据源生成**,不手写:手写的那份会和锁读的那份漂开,
   而漂开之后两边各自都"看起来对"。
"""
import io
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

import census        # noqa: E402
import classified    # noqa: E402

HEADER = """# WO_206 · DeepSeek 官方线模型名发出点签入表(2026-09-14)

- 窗口:C(agentscope-07-14) · 分支 `fix/c14-206c1-deepseek-flash-20260914` · 基线 `4152dfe16`
- **枚举靠脚本,分类靠人读。** 枚举:`tests/deepseek_official_line_2026_09_14/census.py`
  (分母 = `git ls-files '*.py'`,不是手抄清单);分类:同目录 `classified.py`,每行一句依据。
- 本文件**由 `render_table.py` 生成**,不手写 —— 手写的那份会和锁读的那份漂开。

## 🔴 2026-09-14 返工:分母重算(44 → 54)

第一版的枚举锚是**形状猜测** —— 只认 `model=` / `"model":` / `default_model=` /
`DEFAULT_*MODEL=` 这几种写法。拿「引号里以 `deepseek-` 开头的字面量」当上界复量:
它命中 135 处,而**生产代码里另有 126 处它从没看见**,其中确有真发出点:

| 漏掉的 | 为什么是发出点 |
|---|---|
| `services/placement_service.py:945` | 同元组里就是 `https://api.deepseek.com/v1/chat/completions` + 官方 key |
| `tools/pricing_llm_assessor.py:229` | 直接传官方 URL,走官方 key 池 failover |
| `writing/llm_providers.py:40` | 该条目 `api_url` 是官方线,`model_id` 是真发出去的名字 |
| `agents/provider_config.py:38` | `OpenAIChatModel(…, base_url=api.deepseek.com/v1)` |
| `services/flywheel_judgment.py:39` | 元组第一位 `'deepseek'` = 官方直连(**第 40 行紧挨着是百炼同名行**) |
| `agents/report_enhancement_agent.py:175` | 同一条线的 `or "deepseek-chat"` 兜底 |

漏掉的写法:`model_id=` `"model_id":` `model_name=` `MODEL_NAME=` `MODEL_MAIN=`
`default_model_key=` `: str = "…"` `or "兜底"`、以及**位置参数**(46 处)。

教训:**先列形态,再写锚**。现在 census 枚举**所有**像模型名的字面量,
本表要对**每一条**签字 —— 包括「它不是发出点」这句话本身。
「这不是发出点」必须是**签了字的话**,不能是锚没覆盖造成的**沉默**。

反向的一条同样要紧:`services/research_monitor/platforms.py:491` 看着像官方线,
但同段写着「官方通道 fail-closed,绝不静默回落百炼 —— 那正是以为在测 DeepSeek
实际测阿里的成因」⇒ 它是**被监测引擎**,范围 b,**不许改**。

## 为什么不自动判供货线

试过「往上找最近的网关标记」这个启发式,抽核当场被骗两次:

1. `writing/llm_utils.py:50` 上方注释写着「落回 dashscope/qwen3.6-plus」——
   那段代码其实走官方线。注释里提到另一条线是家常便饭(它们往往正是在解释
   "为什么不走那条")。
2. 跳过注释之后,又因为三条供货线的代码彼此挨着,判成了 openrouter。

**自动判供货线在本仓产出的是自信的错答案。** 而错答案的后果不是"少改一处",
是把百炼那条链改成一个它不认识的名字 —— 只在运行时报「模型不存在」,
或者更糟,静默落到别的档。所以分类逐行人读。

## 口径

| 字段 | 取值 |
|---|---|
| kind | `emit` 真发出去的模型名 · `registry_key` 注册表键(经二次解析)· `option` 面向用户的可选项 · `compare` 比较/分档判断 · `label` 记账/展示标签 · `price_row` 计价表行 · `const` 常量 SSOT 自身 · `db_default` 库默认值/种子 · `doc` 注释 |
| provider | `official`(api.deepseek.com / 官方 key 池)· `dashscope`(百炼,**另一家的模型 ID**)· `openrouter` · `registry`(经注册表二次解析)· `mixed` · `n/a` |
| scope | `a` GEO 干活模型(本单范围)· `b` 被监测引擎(换它=换被测对象,不动)· `c` 社媒/IP(板块边界)· `x` 脚本/判据(非生产) |
| plan | `switch` 本单要改 · `keep` 不动 · `later` 后续单 |

"""


def _rows():
    """本表的范围 = **判决说它是 a 或 b 的行**,不是「路径看起来像 GEO 的行」。

    🔴 两者会打架,而打架的时候路径是错的那个:
       `services/llm/deepseek_key_pool.py` 路径在社媒目录下,
       实际是 **GEO 官方 key 池**(c1a 还改过它)。按路径取,它根本进不了这张表,
       于是一个我改过的文件在"签入表"里查无此行 —— 表就开始骗人了。
       路径只是**默认值**,人读的判决才是权威。
    """
    out = []
    for r in census.census("."):
        v = classified.verdict_for(r)
        if v and v[2] in ("a", "b"):
            out.append(r)
    return out


DOC_PATH = "docs/AI-CONTEXT/DEEPSEEK_OFFICIAL_EMIT_SITES_2026-09-14.md"


def main(out_path=None):
    """`out_path` 为空才写签入文件。

    🔴 [Review 09-14 · P9] 原来 in-sync 判据的做法是「先渲染、再比对」——
       而渲染是**覆盖签入文件**。于是 census 一旦坏掉(比如枚举不到任何行),
       它会先把表冲成空的、再和空的比对,然后**报绿**。
       仪器坏了不该报绿,更不该顺手把证据一起改掉。现在判据渲染到临时文件比对。
    """
    rows = _rows()
    buckets = {}
    for r in rows:
        v = classified.verdict_for(r)
        assert v is not None, "表里没有 %s %s 第 %d 处" % (r["path"], r["model"], r["occ"])
        kind, provider, scope, reason, plan = v
        buckets.setdefault(plan, []).append((r, kind, provider, scope, reason))

    out = [HEADER]
    counts = {k: len(v) for k, v in buckets.items()}
    out.append("## 总数\n")
    out.append("GEO 干活线上的模型名字面量共 **%d** 条(scope=a 与 b),"
               "其中 **%d 条要改**、%d 条不动、%d 条留后续单。\n"
               % (len(rows), counts.get("switch", 0),
                  counts.get("keep", 0), counts.get("later", 0)))
    out.append("> 工单原写「11 条路径」。那 11 条都在下面的 `switch` 里,"
               "但只占五分之一 —— **别人给的份数不是分母**。\n")
    emit = len([1 for v in buckets.values() for x in v if x[1] == "emit"])
    out.append("> 其中 `kind=emit`(真发出去的名字)**%d** 条;"
               "其余是 registry_key / compare / label / doc —— 它们**不发名字**,"
               "但都逐条签了字,不是锚没覆盖造成的沉默。\n" % emit)

    titles = {"switch": "本单要改(switch)", "keep": "一个字不动(keep)",
              "later": "留后续单(later)"}
    for plan in ("switch", "keep", "later"):
        items = buckets.get(plan) or []
        if not items:
            continue
        out.append("\n## %s · %d 条\n" % (titles[plan], len(items)))
        # [0913e 集成] 表里**不写行号**:行号会随任何一次合车整体平移,
        # 于是这张签入表在 206 分支树与 0913e 合成树上长得不一样,
        # in-sync 判据每合一次车就红一次 —— 而"为无关原因红"的锁最后会被放宽。
        # 定位用 `第 N 处`(该文件内该名字的第几次出现),grep 名字即可找到。
        out.append("| 文件 | 当前模型名 | 第 N 处 | kind | 供货线 | 用途 | 分类依据(人读) |")
        out.append("|---|---|---|---|---|---|---|")
        for r, kind, provider, scope, reason in sorted(
                items, key=lambda x: (x[0]["path"], x[0]["model"], x[0]["occ"])):
            out.append("| `%s` | `%s` | %d | %s | %s | %s | %s |"
                       % (r["path"], r["model"], r["occ"], kind, provider,
                          scope, reason))

    out.append("\n## 反向对照:**不是官方线**的那些名字\n")
    other = other_line_names(rows)
    out.append("百炼 / OpenRouter / 注册表键那一侧,共 **%d** 条,涉及 %d 个文件。\n"
               % (sum(n for _, _, n in other), len({p for p, _, _ in other})))
    out.append("🔴 判据冻结的是**内容**不是计数:Review 的 Q3 实测过 ——"
               "把一处百炼名改掉,**计数不变、内容变了**,只冻计数的那条锁不会红。"
               "所以下面这张 `(文件, 名字, 条数)` 表要逐行相等。\n")
    out.append("| 文件 | 名字 | 条数 |")
    out.append("|---|---|---|")
    for path, model, n in other:
        out.append("| `%s` | `%s` | %d |" % (path, model, n))

    io.open(out_path or DOC_PATH,
            "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("已生成 · switch=%d keep=%d later=%d · 非官方线内容表=%d 行"
          % (counts.get("switch", 0), counts.get("keep", 0),
             counts.get("later", 0), len(other)))


def other_line_names(rows=None):
    """**不是官方线**的那些名字,按 `(文件, 名字, 条数)` 排序返回。

    🔴 不带行号:同文件里加删几行就会让行号整体平移,而一条为无关原因转红的
       冻结表,下一个人只会把它放宽。`(文件, 名字, 条数)` 对「名字被改了」
       同样敏感(Q3 那发毒:百炼名一改,这张表就对不上),却不会被行号平移带红。
    """
    rows = _rows() if rows is None else rows
    counter = {}
    for r in rows:
        v = classified.verdict_for(r)
        if v and v[1] in ("dashscope", "openrouter", "registry"):
            counter[(r["path"], r["model"])] = counter.get((r["path"], r["model"]), 0) + 1
    return [(p, m, n) for (p, m), n in sorted(counter.items())]


def official_keep_names(rows=None):
    """**官方线上标了 keep** 的那些名字,按 `(文件, 名字, 条数)` 返回。

    🔴 [L3 · Review 09-14] 反向对照原来只冻「非官方线」,于是官方线上那些
       **故意不动**的行(尤其 `deepseek-v4-pro`)没人看着 —— 把它们改成 flash
       只有 in-sync 一条红,而 in-sync 的提示语引向「重新渲染」,照做就全绿了。
       v4-pro 是官方今天**还在供应**的另一档:改它是降档,贵/慢/质量三件事一起变。
    """
    rows = _rows() if rows is None else rows
    counter = {}
    for r in rows:
        v = classified.verdict_for(r)
        if v and v[1] == "official" and v[4] == "keep":
            counter[(r["path"], r["model"])] = counter.get((r["path"], r["model"]), 0) + 1
    return [(p, m, n) for (p, m), n in sorted(counter.items())]


if __name__ == "__main__":
    main()
