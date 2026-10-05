# -*- coding: utf-8 -*-
"""WO_217-c1a · 全 provider 模型名普查(c1b 的分母,也是改动前的地图)。

Owner 直令(2026-09-15 12:34 北京):「生产力模型全部默认 deepseek-flash,
其他的除非必要,比如需要阿里或者豆包的模型,或者我们的监测」。

本模块**只普查,不切换**。它回答一个问题:
    **这个仓里,一共有多少处"在这里指定了一个模型"?**

═══════════════════════════════════════════════════════════════════════
🔴 为什么不照工单 §1 那张表做
   Review 按 `e7669e699` 读出约 40 个任务键。**别人给的份数不是分母**
   —— 那张表是按 `settings_manager` / `model_config` 两个文件读的,
   而模型名散落在 provider 客户端、agent 默认参数、`or` 兜底、位置参数里。
   所以这里自己机械枚举,再拿工单那张表**反过来核**(见 test 的覆盖腿)。

🔴 锚怎么来的:**先列形态,再写锚**(206 的血教训 —— 第一版拿
   `model=` / `"model":` 这几种写法当筛子,漏了 126 处真发出点)。
   做法是先 AST 收全仓 36 万条字符串字面量,人读"长得像模型 id"的那 1630 条,
   再据此写下面这个**按厂商前缀**的并集。左边长什么样一律不问。

🔴 锚一定漏。所以本模块给**三条腿**,第三条专门用来量"第一条漏了多少":
     L 字面量腿  —— 厂商前缀并集(主分母)
     C 常量引用腿 —— `DEEPSEEK_OFFICIAL_*` 等常量的**使用处**
                    (不算它,每切换一处分母就少一处,表会自己空掉,
                     而"空表全绿"和"全部覆盖"长得一模一样)
     K 键腿/校准 —— AST:凡是被绑在名字含 model/engine/llm 的键上的字面量
                    K 里有而 L 里没有的,就是**我的前缀并集不认识的厂商**,
                    必须逐条签字,不许沉默。
═══════════════════════════════════════════════════════════════════════
"""
import ast
import io
import os
import re
import subprocess

# ──────────────────────────────────────────────────────────────────
# L 腿:厂商前缀并集
# ──────────────────────────────────────────────────────────────────
#: 每一项都来自 `shape_discover` 的实测清单,不是我凭印象写的。
#: 允许 openrouter 风格的 `厂商/模型` 前缀(anthropic/claude-… google/gemini-…)。
#: 厂商名后必须还有东西,用来排掉 provider 名本身("deepseek" / "kimi" 这种
#: 出现在 `provider="deepseek"` 里的字符串不是模型名)。
_VENDORS = (
    "deepseek", "qwen", "kimi", "moonshot", "doubao", "claude", "gpt",
    "gemini", "text-embedding", "glm", "ernie", "baichuan", "llama",
    "mistral", "grok", "hunyuan", "minimax", "abab", "step", "yi",
)

#: 🔴🔴 2026-09-15 **返工:这条锚第一版漏掉了整单的主角**。
#:   原文是 `(?:厂商)[-.]`,要求厂商名后**紧跟**分隔符。
#:   而阿里的写法是 `qwen3.7-max` —— `qwen` 后面跟的是版本数字 `3`。
#:   于是 `qwen3.7-max`(37 个文件的默认值)、`qwen3-max`、`qwen3.6-plus`、
#:   `qwen3-asr-flash` **整族一条没进表**,而表看起来是满的。
#:   抓住它的不是我读代码,是 K 腿(校准腿)—— 它把 8 个 qwen3.x 名字
#:   当作"前缀并集认不出的"喊了出来。这正是 K 腿存在的唯一理由:
#:   **锚一定会漏,漏了必须出声**(本仓 `make-the-instrument-shout-when-it-breaks`)。
#:   所以厂商名后面现在允许一段版本号,再要求分隔符 + 至少一个字母数字。
_MODEL_LITERAL = re.compile(
    r'["\']((?:[a-z]+/)?(?:' + "|".join(_VENDORS) +
    r')[0-9]*(?:\.[0-9]+)?[-./][A-Za-z0-9][A-Za-z0-9._/\-]*)["\']',
    re.IGNORECASE)

#: 上面那条锚会把 `CLAUDE.md` `moonshot.cn` 这类**文件名/域名**也算进来
#: (`claude` + `.` + `md`)。按后缀排掉 —— 这是纯词法判断,不涉及语义。
_NOT_A_MODEL_SUFFIX = (
    ".md", ".py", ".json", ".txt", ".yml", ".yaml", ".html", ".csv",
    ".sql", ".sh", ".log", ".cn", ".com", ".ai", ".io", ".net", ".org",
)


def _is_model_shaped(name):
    return not name.lower().endswith(_NOT_A_MODEL_SUFFIX)

#: 火山方舟的 endpoint id 形如 `ep-20240xxx-xxxxx`,是**模型的另一种指定方式**,
#: 不写进去会漏掉整条豆包线。
_ARK_ENDPOINT = re.compile(r'["\'](ep-[0-9a-z]{6,}[-0-9a-z]*)["\']')

# ──────────────────────────────────────────────────────────────────
# C 腿:常量引用
# ──────────────────────────────────────────────────────────────────
_CONST_VALUES = {
    "DEEPSEEK_OFFICIAL_FLASH": "deepseek-flash",
    "DEEPSEEK_OFFICIAL_PRO": "deepseek-v4-pro",
}
_CONST_REF = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(_CONST_VALUES) + r")(?![A-Za-z0-9_])")
_CONST_HOME = "config/deepseek_models.py"

# ──────────────────────────────────────────────────────────────────
# 供货线:按**同文件出现的网关**认,不按模型名猜
# (模型名猜供货线会错:`deepseek-v4-flash` 在百炼上是另一家的 ID)
# ──────────────────────────────────────────────────────────────────
_GATEWAY_MARKS = [
    ("deepseek_official", ("api.deepseek.com", "deepseek_key_pool",
                           "adeepseek_post_with_failover", "DEEPSEEK_API_KEY")),
    ("dashscope",         ("dashscope", "DASHSCOPE", "aliyuncs.com",
                           "compatible-mode", "百炼")),
    ("openrouter",        ("openrouter", "OPENROUTER")),
    ("moonshot",          ("api.moonshot.cn", "MOONSHOT", "KIMI_API_KEY")),
    ("ark_doubao",        ("ark.cn-beijing.volces.com", "DOUBAO_", "ARK_API_KEY",
                           "volces")),
    #: 🔴 siliconflow 是我**第二次**被 `head -N` 截掉的东西:
    #:   端点扫描按出现次数取前 30,它次数少排在后面;
    #:   是 K 腿把 `siliconflow-deepseek-v3` 这个 model_key 喊出来才补上的。
    ("siliconflow",       ("siliconflow", "SILICONFLOW")),
]


# ──────────────────────────────────────────────────────────────────
# M 腿:`LLM_PROVIDERS` 注册表的 **model_key**(第三套命名)
#   形如 `dashscope-deepseek-v4-pro` —— 既不是厂商前缀开头,也不是 ark endpoint,
#   L 腿一条都看不见。而 `server.py:15232` 写文章的默认值就是这种写法:
#       model_key: str = "dashscope-deepseek-v4-pro"
#   c1b 换线时**必须**连这套一起换,否则改了 model_id 而 caller 还在按旧 key 取配置。
#   🔴 键**从源码 AST 读**,不手抄 —— 手抄的清单会和注册表分叉。
# ──────────────────────────────────────────────────────────────────
_REGISTRY_HOME = "writing/llm_providers.py"
_REGISTRY_NAME = "LLM_PROVIDERS"


def model_registry_keys(root="."):
    """AST 读出 `LLM_PROVIDERS` 的键 -> {key: model_id}。"""
    out = {}
    try:
        tree = ast.parse(io.open(os.path.join(root, _REGISTRY_HOME),
                                 encoding="utf-8").read())
    except Exception:
        return out
    for n in ast.walk(tree):
        tgt = None
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            tgt = n.target.id
        elif isinstance(n, ast.Assign) and n.targets and isinstance(n.targets[0], ast.Name):
            tgt = n.targets[0].id
        if tgt != _REGISTRY_NAME or not isinstance(getattr(n, "value", None), ast.Dict):
            continue
        for k, v in zip(n.value.keys, n.value.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                continue
            mid = None
            if isinstance(v, ast.Call):
                for kw in v.keywords:
                    if kw.arg == "model_id" and isinstance(kw.value, ast.Constant):
                        mid = kw.value.value
            out[k.value] = mid
    return out

# ──────────────────────────────────────────────────────────────────
# 用途分域(按路径)。域决定这一处**该不该切**,不决定它是不是模型名。
# ──────────────────────────────────────────────────────────────────
#: 社媒线 —— Owner 2026-09-15 明确排除(「社媒不用管」),不是我判的。
#: [开源 E3 · B2] 社媒工具包目录、社媒 agent 两项随包整删,从分域表去掉(再点名已删路径不分出任何东西)
_SOCIAL = ("api/social_", "api/content_api.py", "scripts/product/social_")
#: 监测被测引擎 —— 换了它就不是在监测那家引擎了。
_MONITORED_ENGINE = ("tools/monitoring/", "services/ai_surface_monitoring/",
                     "services/research_monitor/", "tools/ai_visibility",
                     "api/monitoring_api.py")
_NOT_PRODUCTION = ("scripts/", "tests/", "agent-test-artifacts/")


def scope_of(path):
    p = path.replace("\\", "/")
    if any(p.startswith(x) or ("/" + x) in p for x in _NOT_PRODUCTION):
        return "not_production"
    if any(x in p for x in _MONITORED_ENGINE):
        return "monitored_engine"
    if any(x in p for x in _SOCIAL):
        return "social_owner_excluded"
    return "geo_work"


def file_gateways(text):
    """这个文件里出现过哪些网关(可能多条线共存 —— 那就要逐行看)。"""
    return tuple(name for name, marks in _GATEWAY_MARKS
                 if any(m in text for m in marks))


def nearest_gateway(lines, idx, window=140):
    """从第 idx 行往上找最近的网关标记。**只是提示**,分类以人读为准。"""
    for j in range(idx - 1, max(-1, idx - window) - 1, -1):
        code = lines[j].split("#", 1)[0]
        s = lines[j].strip()
        if s.startswith("#") or s.startswith('"""') or s.startswith("'''"):
            continue
        for name, marks in _GATEWAY_MARKS:
            if any(m in code for m in marks):
                return name
    return "unresolved"


def is_comment_line(line):
    """纯注释行。唯一被允许的自动判断 —— 这是语法问题,不是语义问题。"""
    return line.strip().startswith("#")


def _is_import(line):
    """[已弃用] 按行首判 import —— **看不见多行 import 的续行**。

    保留定义是因为它曾是唯一的判别法;真正在用的是 `import_line_numbers()`。
    """
    t = line.strip()
    return t.startswith("from ") or t.startswith("import ")


def import_line_numbers(text):
    """这个文件里**属于 import 语句**的全部行号(含多行 import 的每一行)。

    🔴 [WO_221-c1 修] 原来按行首判 `from ` / `import `,于是

            from config.deepseek_models import (
                DEEPSEEK_OFFICIAL_FLASH,      <- 这一行不以 from 开头
            )

        的续行被当成**常量引用**数进分母。实测:`tools/ai_visibility/ai_tester.py`
        因此从 17 虚增到 18,两侧冻结当场红 —— **冻结把仪器的缺陷变成了红**,
        这正是两侧锁存在的理由(本仓 a-frozen-baseline-turns-instrument-breakage-into-red)。

        改成用语法树问「这一行属不属于一条 import 语句」:多行、带括号、
        带 as 别名的写法一律认得,不依赖它长什么样。
    """
    try:
        tree = ast.parse(text)
    except Exception:
        return set()
    lines = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for i in range(n.lineno, (n.end_lineno or n.lineno) + 1):
                lines.add(i)
    return lines


def left_shape(line, name):
    """字面量左边那一小段,归成可读的"写法"标签(给人读表用)。"""
    k = -1
    for q in ('"', "'"):
        k = line.find(q + name + q)
        if k >= 0:
            break
    if k < 0:
        return "?"
    left = line[:k].rstrip()
    for pat, lab in (
        (r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*$', lambda m: m.group(1) + "="),
        (r'["\']([A-Za-z_][A-Za-z0-9_]*)["\']\s*:\s*$', lambda m: '"' + m.group(1) + '":'),
        (r'([A-Za-z_][A-Za-z0-9_.]*)\s*\(\s*$', lambda m: m.group(1) + "()"),
    ):
        m = re.search(pat, left)
        if m:
            return lab(m)
    if left.endswith("or"):
        return "or 兜底"
    if left.endswith(("(", ",", "[")):
        return "位置参数/元素"
    return "其它"


#: 🔴🔴 [c1a' 2026-09-15] **仪器自身不进分母**。
#:   c1a 交付时冻结 total_all=1085,是在**提交本判据包之前**量的。
#:   提交之后 `git ls-files *.py` 把仪器自己数了进去(census.py / classified.py /
#:   test_model_census.py / poison_217_c1a.py 里全是模型名字面量)⇒ 1117,
#:   于是这条判据在**任何干净 checkout 上都红**,而红的理由与被测对象无关。
#:   生产四项(586/470/56/60)当时逐字未变 —— 因为仪器落在 tests/ 与 scripts/,
#:   被 scope_of 判成 not_production;只有全仓那一项被污染。
#:   🔴 这是「量尺把自己也量了进去」:分母必须排除**做测量的那几个文件**,
#:      而排除集本身要被判据钉住,否则它会慢慢变成一个藏东西的地方。
_INSTRUMENT_FILES = (
    "tests/model_line_flash_census_2026_09_15/",   # 本判据包(整目录)
    "scripts/poison_217_c1a.py",                   # 本包的注毒台
)


def is_instrument(rel):
    """这个文件是不是**本普查器自己**(做测量的那几个)。"""
    rel = rel.replace("\\", "/")
    return any(rel == x or rel.startswith(x) for x in _INSTRUMENT_FILES)


# ──────────────────────────────────────────────────────────────────
# 按**最近凭据**取证的归属法(比 `gateway` 字段硬)
# ──────────────────────────────────────────────────────────────────
#: 🔴 为什么不用上面那个 `gateway` / `nearest_gateway`:
#:   它按「同文件出现过哪些网关标记」或「往上找最近的网关词」判,而网关词里
#:   `dashscope` 这种字符串到处都是。实测它骗过一次 ——
#:   `services/ai_surface_monitoring/lineage.py:175` 那一面
#:   (`provider_key="deepseek_official"` / `env_key_var="DEEPSEEK_API_KEY"` /
#:   availability=active / default_enabled=True)被它判成 `dashscope`,
#:   而那正是唯一一个**活着的、在污染监测数据的**面。
#:
#: 这里改判**凭据**:发给谁由「带哪把 key / 打哪个 base URL」决定,
#: 不由附近出现过哪个词决定。key 名与域名是硬的,形容词不是。
#: **强证据**:凭据名与域名。它们是「这次调用带哪把 key、打哪个域」的直接痕迹,
#: 在窗口内按距离取最近的一个。
_STRONG_EVIDENCE = [
    ("metaso",            ("METASO_API_KEY",)),
    ("moonshot",          ("KIMI_API_KEY", "api.moonshot.cn")),
    ("ark_doubao",        ("VOLC_API_KEY", "DOUBAO_API_KEY", "ark.cn-beijing.volces.com")),
    ("deepseek_official", ("DEEPSEEK_API_KEY", "api.deepseek.com", "deepseek_key_pool")),
    ("dashscope",         ("DASHSCOPE_API_KEY", "aliyuncs.com", "compatible-mode")),
]

#: 🔴 **弱证据**:provider 标签(`"deepseek_official"` / `"dashscope"` / `"metaso"`)。
#:   它们在血缘表里是**数据值**,不是网关痕迹。只认**同一行** ——
#:   实测:`tools/monitoring/batch_monitor.py` 的 contract 表里,
#:   `"deepseek": ("deepseek_official", …)` 与 19 行之后的百炼兜底行挨得很近,
#:   按「窗口内最近」判,兜底那一行会被判成官方线。**表格里行挨着行,
#:   「最近」会跨记录** —— 同一行才保证说的是同一条记录。
_WEAK_LABELS = {
    "deepseek_official": "deepseek_official",
    "dashscope": "dashscope",
    "metaso": "metaso",
    "volc_ark": "ark_doubao",
    "moonshot": "moonshot",
}


def attribute_by_nearest_credential(path, line, window=45):
    """这一行的模型名,**按最近的凭据证据**判它发给谁。

    返回 (供货线, 证据串, 证据所在行);找不到返回 ("unresolved", "-", 0)。
    只看代码(行尾注释切掉)—— 注释里提到另一条线是允许的。
    """
    try:
        src = io.open(path, encoding="utf-8").read().splitlines()
    except Exception:
        return ("unresolved", "-", 0)
    i = line - 1
    if i < 0 or i >= len(src):
        return ("unresolved", "-", 0)
    # ① 同一行:强弱证据都认 —— 同行必定说的是同一条记录
    same = src[i].split("#", 1)[0]
    for who, marks in _STRONG_EVIDENCE:
        for m in marks:
            if m in same:
                return (who, m, line)
    for label, who in _WEAK_LABELS.items():
        if label in same:
            return (who, label, line)
    # ② 窗口内:只认强证据,按距离取最近
    lo, hi = max(0, i - window), min(len(src), i + window)
    found = []
    for j in range(lo, hi):
        code = src[j].split("#", 1)[0]
        for who, marks in _STRONG_EVIDENCE:
            for m in marks:
                if m in code:
                    found.append((abs(j - i), who, m, j + 1))
    if not found:
        return ("unresolved", "-", 0)
    found.sort()
    return (found[0][1], found[0][2], found[0][3])


def tracked_python_files(root="."):
    """分母 = `git ls-files`,不是我手写的清单;再扣掉仪器自身。"""
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=root,
                         capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", "replace"))
    return [l for l in out.stdout.decode("utf-8").splitlines()
            if l.strip() and not is_instrument(l)]


def census(root="."):
    """返回 `[{path, line, model, shape, comment, gateway, scope, occ}]`。

    每一条 = **一个像模型名的字面量(或一个模型常量的使用处)**,
    不是"我认为的发出点"。是不是发出点、该不该切,由 `classified.py` 逐条签字。
    """
    rows = []
    reg_keys = model_registry_keys(root)
    reg_re = re.compile(
        r'["\'](' + "|".join(re.escape(k) for k in sorted(reg_keys, key=len, reverse=True))
        + r')["\']') if reg_keys else None
    for rel in tracked_python_files(root):
        rel = rel.replace("\\", "/")
        try:
            text = io.open(os.path.join(root, rel), encoding="utf-8").read()
        except Exception:
            continue
        gws = file_gateways(text)
        sc = scope_of(rel)
        lines = text.splitlines()
        import_lines = import_line_numbers(text)
        for i, line in enumerate(lines, 1):
            found = [(m.start(), m.group(1)) for m in _MODEL_LITERAL.finditer(line)
                     if _is_model_shaped(m.group(1))]
            found += [(m.start(), m.group(1)) for m in _ARK_ENDPOINT.finditer(line)]
            #: 🔴 注册表**自己那个文件**不排除:`writing/llm_providers.py:170`
            #:   的 `DEFAULT_MODEL = "dashscope-qwen-max"` 就在里面,
            #:   按文件排除会把这个真发出点一起藏掉。同位置重复由 `seen_at` 去重。
            if reg_re is not None:
                seen_at = {p for p, _ in found}
                found += [(m.start(), m.group(1)) for m in reg_re.finditer(line)
                          if m.start() not in seen_at]
            if rel != _CONST_HOME and i not in import_lines:
                found += [(m.start(), _CONST_VALUES[m.group(1)])
                          for m in _CONST_REF.finditer(line)]
            for _pos, name in sorted(found):
                gw = gws[0] if len(gws) == 1 else (
                    nearest_gateway(lines, i - 1) if len(gws) > 1 else "none")
                rows.append({"path": rel, "line": i, "model": name,
                             "gateway": gw, "file_gateways": ",".join(gws) or "-",
                             "scope": sc, "shape": left_shape(line, name),
                             "comment": is_comment_line(line),
                             "src": line.strip()[:130]})
    #: 不随行号漂的身份号(206 同法):`occ` = 该 (文件, 模型名) 在文件内第几次出现。
    cnt = {}
    for r in sorted(rows, key=lambda r: (r["path"], r["line"])):
        k = (r["path"], r["model"])
        cnt[k] = cnt.get(k, 0) + 1
        r["occ"] = cnt[k]
    return rows


# ──────────────────────────────────────────────────────────────────
# K 腿(校准):AST 取"被绑在 model 类名字上的字面量"
#   它的用途**只有一个** —— 量出 L 腿的前缀并集漏了哪些厂商。
# ──────────────────────────────────────────────────────────────────
_KEYISH = re.compile(r"(?i)(model|engine|llm)")


def keyed_literals(root="."):
    """返回 `{字面量: {出处}}`,凡绑在名字含 model/engine/llm 的键上。"""
    out = {}

    def add(v, rel, lineno):
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            s = v.value.strip()
            if 2 <= len(s) <= 48 and " " not in s:
                out.setdefault(s, set()).add("%s:%d" % (rel, lineno))

    for rel in tracked_python_files(root):
        rel = rel.replace("\\", "/")
        if scope_of(rel) == "not_production":
            continue
        try:
            tree = ast.parse(io.open(os.path.join(root, rel), encoding="utf-8").read())
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    nm = t.id if isinstance(t, ast.Name) else (
                        t.attr if isinstance(t, ast.Attribute) else "")
                    if nm and _KEYISH.search(nm):
                        add(n.value, rel, n.lineno)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                if _KEYISH.search(n.target.id) and n.value is not None:
                    add(n.value, rel, n.lineno)
            elif isinstance(n, ast.keyword):
                if n.arg and _KEYISH.search(n.arg):
                    add(n.value, rel, n.lineno)
            elif isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                            and _KEYISH.search(k.value):
                        add(v, rel, n.lineno)
    return out


def literal_names(rows):
    return {r["model"] for r in rows}


def calibration_gap(root="."):
    """K 腿里有、L 腿的前缀并集**认不出**的字面量 —— 必须逐条签字。"""
    rows = census(root)
    known = {m.lower() for m in literal_names(rows)}
    gap = {}
    for s, wheres in keyed_literals(root).items():
        if s.lower() in known:
            continue
        if not _MODEL_LITERAL.match('"%s"' % s) and not _ARK_ENDPOINT.match('"%s"' % s):
            gap[s] = wheres
    return gap


if __name__ == "__main__":
    rows = census(".")
    prod = [r for r in rows if r["scope"] != "not_production"]
    print("模型名出现处(全仓 git ls-files *.py):%d" % len(rows))
    print("  其中生产代码(去掉 scripts/tests):%d" % len(prod))
    by_scope = {}
    for r in prod:
        by_scope.setdefault(r["scope"], []).append(r)
    for k in sorted(by_scope):
        print("    %-24s %d 处 · %d 个文件"
              % (k, len(by_scope[k]), len({r["path"] for r in by_scope[k]})))
    print()
    gap = calibration_gap(".")
    print("K 腿校准:前缀并集认不出的 %d 条(每条须签字)" % len(gap))
    for s in sorted(gap)[:40]:
        print("   %-40r %s" % (s, sorted(gap[s])[:2]))
