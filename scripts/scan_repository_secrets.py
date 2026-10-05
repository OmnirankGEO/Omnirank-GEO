#!/usr/bin/env python3
"""Fail closed when repository or image files contain credential material.

The known compromised values are represented only by SHA-256 fingerprints.
Generic checks intentionally target high-confidence executable-code patterns;
test fixtures and historical prose still receive the exact-fingerprint check.

secret-scanner-self-exclusion-marker::b3a1c7 —— 本行是扫描器的**自我标识**。
扫描器自己的源码里有整套校准样本(正则、前缀、人造载荷),扫到自己必然满仓红。
🔴 自排除按**文件内容里的这个标记**判,不按文件名:preflight 会把扫描器
复制成 `_scanner.py` 搬进临时目录再跑,按名字排除在那里就失效了。
这个标记只允许出现在本文件里 —— 判据
test_the_self_exclusion_marker_appears_in_exactly_one_repo_file 钉住这一点,
免得它变成「往文件里贴一行就能让扫描器闭嘴」的后门。
"""

from __future__ import annotations

import argparse
import ast
import warnings
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class KnownSecret:
    label: str
    sha256: str
    length: int


KNOWN_SECRETS: tuple[KnownSecret, ...] = (
    KnownSecret(
        label="compromised_production_root_ssh_credential",
        sha256="3794dd731657f824b2d430082e4446bbe5f422ff2b78d292410a295ea7d9fcb8",
        length=21,
    ),
    KnownSecret(
        label="compromised_external_api_key",
        sha256="ef27980ab44edba4822e3723676e583001bab0f2e76b931e82a85bbb18e535ae",
        length=51,
    ),
    KnownSecret(
        label="retired_tv_plaintext_password",
        sha256="ba723435a66e490530c3efdfeac868e06fde6e35dcc43fa8528fb1b2c9411ef5",
        length=6,
    ),
    KnownSecret(
        label="retired_monitoring_clear_password",
        sha256="31856251d5cf4af67e562ba30e302bc5cafa694e344f5cd8b1850ade5eedeebb",
        length=12,
    ),
)

# 🔴 拆成两段再拼:若这里写成完整字面量,本文件里这个标记就出现两次,
#    「标记只准出现在一个文件里」那格锁的读数会含糊。拼接后运行期值不变。
SELF_EXCLUSION_MARKER = "secret-scanner-self-exclusion-marker" + "::b3a1c7"

IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "output",
    "cache",
    "logs",
}
GENERIC_SKIP_PREFIXES = (
    ".planning/",
    ".restore-test/",
    # 🔴 [WO_245] "docs/" 于 2026-09-19 从本元组**移除**。整棵 docs 树曾对所有通用
    #    规则关闭 —— 那不是豁免,是结构性盲区:凭据材料写进 .md 与写进 .py 一样
    #    留在仓里。豁免改为按**规则**做(见 _BEHAVIOURAL_CODES / _looks_like_placeholder),
    #    每条带理由;别再往这个元组里加目录来消音。
    # 🔴 [WO_245 三轮] "tests/" 与 "frontend/tests/" 于 2026-09-19 一并移除 ——
    #    与 docs/ 同形的结构盲区。「以凭据为被测物」的判据文件改由
    #    SECRET_FIXTURE_PATHS 点名豁免(带可验证的理由),不再按目录关闭。
    "frontend/dist/",
    "scripts/product/",
)

# 只在「可执行源码」里成立的规则。理由:它们描述的是**代码会做什么**
# (AutoAddPolicy 会真的关掉 host key 校验),散文引用它不会执行。
# 与之相对,凭据**材料**类规则(PRIVATE_KEY / CREDENTIAL_URL / HARDCODED_*)
# 在任何文件里后果相同 —— 那些不进这张表。
_BEHAVIOURAL_CODES = {"SSH_AUTO_ADD_POLICY"}
_EXECUTABLE_SUFFIXES = {
    ".py", ".sh", ".bash", ".zsh", ".ps1", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".rb", ".pl", ".go", ".rs", ".java",
}
PRIVATE_KEY_FIXTURE_PATHS = {
    "tests/ai_ops/test_ai_ops_codex_runner.py",
    "tests/ai_ops/test_ai_ops_redaction.py",
}

# [WO_245 第三轮] 「以凭据为被测物」的判据文件。
#
# 这类文件**必须**含有形状逼真的凭据载荷 —— 否则它验证的那条规则根本不会响。
# 它们不是「tests/ 目录所以豁免」(那是目录豁免,已经被否了),而是与
# `scan_repository_secrets.py` 排除自己**同一个理由**:被测物就是凭据本身。
#
# 🔴 判据 test_every_secret_fixture_path_really_handles_secrets 钉住这张表:
#    每个条目必须真的引用「密钥扫描器」或「脱敏模块」,拿它当普通豁免会红。
SECRET_FIXTURE_PATHS = {
    "tests/security/test_repository_secret_gate.py",
    "tests/security/test_env_default_secret_rule.py",
    "tests/ai_ops/test_ai_ops_redaction.py",
    "tests/ai_ops/test_ai_ops_chat_tools.py",
    "tests/ai_ops/test_ai_ops_chat_agent.py",
    # 🔴 test_ai_ops_codex_runner.py **不在**这张表里。
    #    它在既有的 PRIVATE_KEY_FIXTURE_PATHS 里,我起初顺手也加了进来,
    #    被上面那条锁当场逮住(它既不引用扫描器也不引用脱敏模块)。
    #    实测:移出豁免后它命中 0 条 —— 根本不需要。
    #    ⇒ 锁红了要先量「是不是真需要」,不是放宽锁去迁就名单。
}
# 上表条目必须命中其中之一,才算「真的以凭据为被测物」。
SECRET_FIXTURE_EVIDENCE = ("scan_repository_secrets", "redaction", "redact")

# RFC 2606 / RFC 6761 保留域 + 回环。指向这些主机的 `user:pass@` 不可能是真凭据 ——
# 这些域**永远不会被解析到真实服务**,是标准明文规定的文档/测试用域。
# 这是**值的形状**规则,不是目录规则:它在 docs、tests、生产代码里一视同仁。
_RESERVED_HOST_RE = re.compile(
    r"""(?ix) ^ (?:
        localhost | 127\.0\.0\.1 | \[::1\] |
        (?:.*\.)? (?: example\.(?:com|net|org|invalid|test) ) |
        (?:.*\.)? (?: invalid | test | localhost | example ) |
        example
    ) $ """
)


@dataclass(frozen=True)
class TransitionalExposure:
    """一条**尚未拆除**的存量暴露,过渡期不让它卡住镜像构建。

    🔴 这不是豁免,是**过渡期不变式**(见 commit 说明)。三条纪律:
      1. `count` 是**精确**期望值。实测多于它 ⇒ 红(同一文件里又新写了一处);
         实测少于它 ⇒ 也红(已经拆了,这条登记必须同班删掉)。
         两个方向都红,才不会变成「登记过就没人回头看」的既成事实。
      2. `tracking` 必须指向真正在拆它的那张单,不能是空话。
      3. 规则本身照常在 `scan_text` 里响 —— 这里只在**闸口**层面放行。
         单元臂看见的仍是原始读数,判据不会因为登记而变绿。
    """

    path: str
    code: str
    count: int
    reason: str
    tracking: str


TRANSITIONAL_EXPOSURES: tuple[TransitionalExposure, ...] = (
    # [WO_247 · 2026-09-20] server.py / api/content_api.py / services/intake_events.py
    # 三条 ENV_DEFAULT_SECRET 登记已随 WO_246 拆除**同班删除**。
    # 🔴 退役是被闸口自己逼出来的,不是记得:WO_246 合车后这三条变成陈账,
    #    tracked 当场 rc=1 报「登记 1 实测 0;WO_246(C)」三行,挡住了车。
    #    这正是登记表设计时要的行为 —— 少删一条就还会红。
    # ---------------------------------------------------------------------
    # [WO_247 · 2026-09-22 已完成] 那 10 处 GitHub 凭据副本(classic PAT ×6 · fine-grained ×4,
    #   实为 2 个不同的值)已在同一笔里全部替换为 <REVOKED_2026-09-22>,本登记随之删除。
    #   吊销由 Owner 于 2026-09-22 在 GitHub 完成;Deploy 侧以 GET /user 独立复核两枚均 401
    #   (反向对照:编造令牌同样 401,证明 401 不是权限问题)。
    #   🔴 历史提交里那几份改不动 —— 本次只清工作树,history rewrite 由 OSS_04/Owner 定。
    # ---------------------------------------------------------------------
)

# 还没拿到单号的登记条数。Review 给号后这个数必须降到 0 ——
# 判据钉住它,免得「待定」成为永久状态。
PENDING_TRACKING_COUNT = 0


# ---------------------------------------------------------------------------
# 判据夹具里的凭据。**这不是过渡登记,是常驻豁免**,两者不要混。
#
# 🔴 必须向复审说明的一点:这一批**做不到「按值形状豁免」**。
#    一个测试账号的口令与一个真口令**形状完全相同** —— 它本来就是照真的造的。
#    实测:tests/ 开闸后 34 条命中,低熵/占位规则能覆盖 **0 条**。
#    能区分它们的只有「这个文件的角色是什么」,而文件角色是路径事实。
#    ⇒ 这里按路径+规则+精确条数登记,每条写明理由;
#      与被否掉的「整目录跳过」的区别是:**逐条点名、计数精确、多一条就红**。
#    若复审坚持不要路径维度的豁免,替代方案只有「把这些夹具值全部换成
#    占位形状」——那要改 12 个测试文件的夹具,超出本单边界,需另开单。
# ---------------------------------------------------------------------------
_FIXTURE_TRACKING = "常驻:判据夹具,被测物即凭据,不拆除"

FIXTURE_CREDENTIALS: tuple[TransitionalExposure, ...] = tuple(
    TransitionalExposure(path=path, code=code, count=count, reason=reason,
                         tracking=_FIXTURE_TRACKING)
    for path, code, count, reason in (
        ("scripts/research/advisory_lock_autocommit_census.py",
         "HARDCODED_CREDENTIAL:LOCK_TOKEN", 1, "advisory lock 的固定标识串 · 按值分不开:它是高熵随机串,与真 token 同形,只有「谁在用它」能区分"),
        ("tests/admin_user_governance/test_governance_extensions.py",
         "HARDCODED_CREDENTIAL:new_password", 1, "改密用例的测试账号口令 · 按值分不开:测试口令照真口令的强度造,形状与真口令完全一致"),
        ("tests/admin_user_governance/test_wallet_missing_does_not_block_the_list_2026_09_07.py",
         "HARDCODED_CREDENTIAL:password", 1, "建测试账号用的口令 · 按值分不开:同上,测试口令与真口令同形"),
        ("tests/ai_ops/test_ai_ops_runner_api.py",
         "HARDCODED_CREDENTIAL:TOKEN", 1, "runner API 用例的假 token · 按值分不开:假 token 要能走通真校验,必须与真 token 同形"),
        ("tests/engine_search_fidelity/test_deepseek_official_surface.py",
         "TOKEN_SHAPE:OPENAI_KEY", 1, "monkeypatch.setenv 注入的假 API key · 按值分不开:它要满足 sk- 前缀与长度才能被被测代码接受"),
        ("tests/geo_observation/test_round3_p1_fixes.py",
         "HARDCODED_CREDENTIAL:_STEAL_TOKEN", 1, "越权用例的攻击者 token 夹具 · 按值分不开:越权用例必须用形状合法的 token 才测得到越权"),
        ("tests/mcidor_2026_08_10/test_marketing_confirm_idor_2026_08_10.py",
         "HARDCODED_CREDENTIAL:VICTIM_TOKEN", 1, "IDOR 用例的受害者 token 夹具 · 按值分不开:同上,形状不合法就到不了被测分支"),
        ("tests/organization_internal_seats/test_invite_delivery_sms_pg.py",
         "HARDCODED_CREDENTIAL:PASSWORD", 1, "邀请用例的测试账号口令 · 按值分不开:测试口令与真口令同形"),
        ("tests/organization_internal_seats/verify_all_accounts_onboarding.py",
         "HARDCODED_CREDENTIAL:password", 2, "批量建测试账号的口令 · 按值分不开:测试口令与真口令同形"),
        ("tests/test_portal_calib_wiring_2026_08_08.py",
         "HARDCODED_CREDENTIAL:SHARE_TOKEN", 1, "门户分享链接用例的 token 夹具 · 按值分不开:分享 token 是高熵随机串,与真 token 同形"),
        ("tests/test_public_report_v2_softauth_2026_08_08.py",
         "HARDCODED_CREDENTIAL:SHARE_TOKEN", 1, "门户分享链接用例的 token 夹具 · 按值分不开:分享 token 是高熵随机串,与真 token 同形"),
        ("tests/test_tv_access.py",
         "HARDCODED_CREDENTIAL:TEST_PASSWORD", 1, "TV 门禁用例口令夹具 · 按值分不开:测试口令与真口令同形"),
        ("tests/test_tv_access.py",
         "HARDCODED_CREDENTIAL:LEGACY_TOKEN", 1, "TV 门禁旧 token 夹具 · 按值分不开:legacy token 是高熵随机串,与真 token 同形"),
        # 🔴 这一条不是夹具,是**槽位名**:api/dashboard_api.py:745 的注释写明
        #    它「存 system_config」,常量里放的是那一行的键名,不是令牌值。
        #    这里选择**登记**而不是加豁免规则 —— 试过的值形状规则
        #    (「小写带分隔符的标识符」)会连 `admin_password` 一起放过,
        #    为了消一条命中而打开一类真缺陷,不划算。
        ("api/dashboard_api.py",
         "HARDCODED_CREDENTIAL:TV_TOKEN_KEY", 1,
         "system_config 的键名非令牌值(同行注释自证) · 按值分不开:试过的「小写带分隔符标识符」规则会连 admin_password 一起放过"),
    )
)

QUOTED_VALUE_RE = re.compile(r"""(?P<quote>['"])(?P<value>[^'"\r\n]{1,})(?P=quote)""")
TOKEN_RE = re.compile(r"[A-Za-z0-9_~!@#$%^&*+=:;,.?/\\-]{12,}")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
CREDENTIAL_URL_RE = re.compile(r"https?://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE)
_CREDENTIAL_URL_VALUE_RE = re.compile(
    r"https?://(?P<user>[^/\s:@]+):(?P<value>[^@\s/]+)@(?P<host>[^/\s'\"]*)",
    re.IGNORECASE,
)
AUTO_ADD_POLICY_RE = re.compile(r"\b(?:paramiko\.)?AutoAddPolicy\s*\(")
HARDCODED_ASSIGNMENT_RE = re.compile(
    r"""(?imx)
    ^\s*(?P<name>[A-Z0-9_]*(?:
        PASSWORD|PASSWD|API_KEY|ACCESS_KEY_SECRET|CLIENT_SECRET|
        PRIVATE_KEY|SECRET_KEY|AUTH_TOKEN
    )[A-Z0-9_]*)\b
    \s*(?::[^=\r\n]+)?=\s*
    (?P<quote>['"])(?P<value>[^'"\r\n]{8,})(?P=quote)
    """
)

# [WO_245 三轮 · a4 补报] ACCESS_KEY_ID 与单独的 TOKEN(含 GH_TOKEN 这类 *_TOKEN)。
#
# 🔴 **故意区分大小写、只认大写常量形**。实测:把裸 TOKEN 并进上面那条
#    大小写不敏感的规则,全仓命中 6 → 42,多出来的 36 条几乎全是测试里的
#    `token = "..."`(门户/分享/认领令牌的夹具标识符,不是凭据)。
#    大写常量 `GH_TOKEN = "..."` 是配置型凭据,小写局部 `token = "..."` 是不透明标识符
#    —— 大小写在这里是有信息量的,不是笔误。
UPPERCASE_TOKEN_ASSIGNMENT_RE = re.compile(
    r"""(?mx)
    ^[ \t]*(?P<name>[A-Z0-9_]*(?:ACCESS_KEY_ID|TOKEN)[A-Z0-9_]*)\b
    \s*(?::[^=\r\n]+)?=\s*
    (?P<quote>['"])(?P<value>[^'"\r\n]{8,})(?P=quote)
    """
)

# 名字里带 TOKEN/KEY 但指的是**种类/状态/用途**,不是值本身。
# `TOKEN_PURPOSE_CONFIRM = "confirm"` 是枚举常量,不是凭据。
_CATEGORISER_SEGMENT_RE = re.compile(
    r"(?:^|_)(?:STATUS|PURPOSE|KIND|TYPE|USE|PREDICATE|LABEL|FIELD|COLUMN"
    r"|HEADER|PARAM|NAME|PREFIX|SUFFIX|PATTERN|REGEX|MODE|SCOPE|REASON"
    r"|EVENT|ACTION|STATE)(?:_|$)"
)

# ---------------------------------------------------------------- WO_245 ----
# 环境变量读取的**兜底字面量**。HARDCODED_ASSIGNMENT_RE 只认 `^NAME = "值"`,
# 认不出 `os.environ.get("X_PASSWORD", "字面量")` —— 口令写在第三个位置参数上,
# 行首没有 NAME=,正则的锚点结构上够不着。两种形状后果相同:仓里有一份明文。
_SECRET_NAME_CORE = r"PASSWORD|PASSWD|SECRET|TOKEN|SALT|API_KEY|ACCESS_KEY|PRIVATE_KEY"
SECRET_ENV_NAME_RE = re.compile(rf"(?i)(?:{_SECRET_NAME_CORE})")

# 被调方白名单:必须是**环境**读取。不能只看 `.get` ——
# `usage.get("completion_tokens", 0)` 是字典取值,名字里带 TOKEN 纯属巧合。
_ENV_GETTER_ATTRS = {"getenv", "environ"}

# 非 .py 文件(以及 .py 解析失败时)的兜底文本臂。
ENV_DEFAULT_RE = re.compile(
    rf"""(?ix)
    (?:os\s*\.\s*)?(?:environ\s*\.\s*get|getenv)\s*\(\s*
    (?P<q1>['"])(?P<name>[A-Z0-9_]*(?:{_SECRET_NAME_CORE})[A-Z0-9_]*)(?P=q1)
    \s*,\s*
    (?P<q2>['"])(?P<value>[^'"\r\n]+)(?P=q2)
    """
)
# ---------------------------------------------------------- WO_245 订正 ----
# 🔴 既有规则**没有一条认凭据的「形态」**:CREDENTIAL_URL 要 `user:pass@` 的 URL 壳,
#    HARDCODED_ASSIGNMENT_RE 要 `NAME = "值"` 的赋值壳,KNOWN_SECRETS 要事先登记过指纹。
#    一个裸的 `ghp_xxxxxxxx…` 三个壳都不占 ⇒ 结构上不可能被报出来。
#    我 09-19 第一版据「既有规则在 docs 里只炸出 21 条无害命中」写了「docs 真泄露 0」——
#    那是拿**我的规则找到的**当分母,不是拿**实际有什么**当分母。实测漏了 10 处。
_TOKEN_SHAPE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # GitHub:classic PAT(ghp_/gho_/ghs_/ghu_/ghr_ + 36)与 fine-grained(github_pat_ + …)。
    ("GITHUB_PAT", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b")),
    ("GITHUB_PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    # [WO_245 三轮 · a4 补报] Google API key 与 Slack token。
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abpr]-[0-9A-Za-z-]{10,}\b")),
)

# RFC 3986 的标准占位对。**两段必须同时**是模板词才放行 ——
# `root:password@` 不满足(root 是真账号名),那条照旧红。
_TEMPLATE_CREDENTIAL_PAIRS = {
    ("user", "pass"),
    ("user", "password"),
    ("username", "password"),
    ("用户名", "密码"),
    ("<user>", "<pass>"),
}

# paramiko 旁边的明文口令。ssh.connect(..., password='…') 一旦带真值,
# 就是一条可直接登录的生产凭据。
PARAMIKO_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+paramiko\b", re.MULTILINE)
PLAINTEXT_PASSWORD_KWARG_RE = re.compile(
    r"""password\s*=\s*(?P<quote>['"])(?P<value>[^'"\r\n]+)(?P=quote)""",
    re.IGNORECASE,
)


def _is_low_entropy_filler(value: str) -> bool:
    """`sk-AAAAAAAAAAAAAAAA` 这种填充样例不是凭据。

    真 token 的随机段字符种类很多;只用几种字符重复堆出来的是**造样例**。
    阈值取 6 —— 一个 36 位真 token 只用 ≤6 种字符的概率可以忽略。
    """
    body = re.sub(r"^(?:gh[pousr]_|github_pat_|sk-(?:ant-)?|AKIA|ASIA)", "", value)
    return len(set(body)) <= 6


ENV_OR_FALLBACK_RE = re.compile(
    rf"""(?ix)
    (?:os\s*\.\s*)?(?:environ\s*\.\s*get|getenv)\s*\(\s*
    (?P<q1>['"])(?P<name>[A-Z0-9_]*(?:{_SECRET_NAME_CORE})[A-Z0-9_]*)(?P=q1)
    \s*\)
    (?:\s*or\s*(?:(?:os\s*\.\s*)?(?:environ\s*\.\s*get|getenv)\s*\(\s*['"][A-Z0-9_]+['"]\s*\))\s*)*
    \s*or\s*
    (?P<q2>['"])(?P<value>[^'"\r\n]+)(?P=q2)
    """
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    code: str


def _normalise_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# 自我命名的占位词:值本身就是「这里该填什么」的名字,不是凭据。
# 只做**全等**比较 —— 用子串会把含 "token" 的真密钥一起放过。
#
# 🔴 这里**故意不收** "password" / "token" / "secret" 这类裸词:
#    它们完全可能是真的弱口令(`https://root:password@...` 就是),
#    收进来等于把一类真缺陷判成占位符。只收「不可能是真值」的写法。
_SELF_NAMING_PLACEHOLDERS = {
    "yourtoken", "your_token", "yourpassword", "your_password",
    "your-token", "your-password", "yourapikey", "your_api_key",
    "密码", "口令", "令牌", "密钥", "你的密码", "你的令牌",
}
# `ghp_xxxx` / `sk-xxxxx` 这类把值本身打了码的写法。
_MASKED_RUN_RE = re.compile(r"x{3,}", re.IGNORECASE)


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    markers = (
        "<redacted",
        "<your",
        "changeme",
        "change_me",
        "example",
        "placeholder",
        "dummy",
        "test-only",
        "test_only",
        "xxxxxxxx",
    )
    return (
        not lowered
        or lowered.startswith(("${", "__"))
        or any(marker in lowered for marker in markers)
        # [WO_245] 尖括号包裹 = 通行的占位约定(`<PAT>` / `<token>`)。
        or (lowered.startswith("<") and lowered.endswith(">"))
        # [WO_245] 值就是它自己那类东西的名字。
        or lowered in _SELF_NAMING_PLACEHOLDERS
        # [WO_245] 值自带打码段。
        or bool(_MASKED_RUN_RE.search(lowered))
    )


def _is_flag_literal(value: str) -> bool:
    """开关默认值,不是凭据材料。

    `os.getenv("TV_ACCESS_PASSWORD_LEGACY_ENABLED", "false")` 名字里有 PASSWORD,
    值是布尔开关 —— 按**值的形状**豁免,不按名字,也不按目录。
    """
    return value.strip().lower() in {
        "0", "1", "true", "false", "yes", "no", "on", "off", "none", "null",
    }


def _is_endpoint_literal(value: str) -> bool:
    """端点默认值(URL / 主机),不是凭据材料。

    注意:带 `user:pass@` 的 URL 由 CREDENTIAL_URL 单独管,这里放行不影响那条。
    """
    stripped = value.strip()
    return stripped.startswith(("http://", "https://", "//")) and "@" not in stripped


def _env_default_is_benign(value: str) -> bool:
    return (
        _looks_like_placeholder(value)
        or _is_flag_literal(value)
        or _is_endpoint_literal(value)
    )


def _looks_like_non_secret_name(name: str) -> bool:
    return name.lower().endswith(
        (
            "_env",
            "_key_name",
            "_storage_key",
            "_jsapi_key",
            "_changed",
        )
    ) or bool(_CATEGORISER_SEGMENT_RE.search(name.upper()))


def _candidate_values(text: str) -> Iterator[tuple[str, int]]:
    seen: set[tuple[int, str]] = set()
    for regex in (QUOTED_VALUE_RE, TOKEN_RE):
        for match in regex.finditer(text):
            value = match.groupdict().get("value") or match.group(0)
            item = (match.start(), value)
            if item not in seen:
                seen.add(item)
                yield value, match.start()


def _is_env_getter_call(node: ast.AST) -> bool:
    """只认环境读取,不认任意 `.get`。

    `os.environ.get(...)` / `environ.get(...)` / `os.getenv(...)` / `getenv(...)`
    都算;`usage.get("completion_tokens", 0)` 不算 —— 被调方是谁才是判据,
    名字里带不带 TOKEN 不是。
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "getenv"
    if isinstance(func, ast.Attribute):
        if func.attr == "getenv":
            return True
        if func.attr == "get":
            base = func.value
            if isinstance(base, ast.Name):
                return base.id == "environ"
            if isinstance(base, ast.Attribute):
                return base.attr == "environ"
    return False


def _secret_env_name(node: ast.AST) -> str | None:
    if not _is_env_getter_call(node):
        return None
    args = node.args  # type: ignore[union-attr]
    if not args or not isinstance(args[0], ast.Constant):
        return None
    name = args[0].value
    if not isinstance(name, str) or not SECRET_ENV_NAME_RE.search(name):
        return None
    return name


def _nonempty_str_constant(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
        return node.value
    return None


def _env_default_hits_ast(text: str) -> list[tuple[int, str, str]] | None:
    """返回 [(行号, 环境变量名, 值)];**解析失败返回 None**(交给文本臂兜底)。

    None 与 [] 必须分得开 —— 「解析不了」和「解析了没命中」是两种状态,
    压成一种就等于让一个语法错误静默关掉这条规则。
    """
    try:
        # 仓里有文件带非法转义序列,ast.parse 会往 stderr 喷 SyntaxWarning。
        # 那是**被扫文件**的事,不是本闸口的读数 —— 不许它污染闸口输出,
        # 否则解析 stderr 的人(复审、CI)会把它当成命中。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        # 形状 A:`getenv("X_PASSWORD", "字面量")` —— 兜底在第二个参数。
        name = _secret_env_name(node)
        if name and len(node.args) >= 2:  # type: ignore[union-attr]
            value = _nonempty_str_constant(node.args[1])  # type: ignore[union-attr]
            if value is not None:
                hits.append((node.lineno, name, value))  # type: ignore[union-attr]
        # 形状 B:`getenv("A") or getenv("B") or "字面量"` —— 兜底在 or 链末端。
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            names = [n for n in (_secret_env_name(v) for v in node.values) if n]
            tail = _nonempty_str_constant(node.values[-1])
            if names and tail is not None:
                hits.append((node.lineno, names[0], tail))
    return hits


def _env_default_hits_text(text: str) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    for regex in (ENV_DEFAULT_RE, ENV_OR_FALLBACK_RE):
        for match in regex.finditer(text):
            hits.append(
                (
                    _line_for_offset(text, match.start()),
                    match.group("name"),
                    match.group("value"),
                )
            )
    return hits


def _env_default_hits(relative: str, text: str) -> list[tuple[int, str, str]]:
    """.py 走 AST(能展开嵌套与跨行),其余走文本臂;.py 解析失败也回落文本臂。

    两臂取**并集**并按 (行号, 变量名) 去重 —— AST 看不见字符串里的代码样例,
    文本臂看不见跨行与嵌套,任何一臂单独用都有结构盲区。
    """
    hits: list[tuple[int, str, str]] = []
    if relative.endswith(".py"):
        parsed = _env_default_hits_ast(text)
        if parsed is not None:
            hits.extend(parsed)
    seen = {(line, name) for line, name, _ in hits}
    for line, name, value in _env_default_hits_text(text):
        if (line, name) not in seen:
            seen.add((line, name))
            hits.append((line, name, value))
    return hits


def _behavioural_checks_enabled(relative: str) -> bool:
    """行为型规则只在可执行源码里成立(见 _BEHAVIOURAL_CODES 的理由)。"""
    return Path(relative).suffix.lower() in _EXECUTABLE_SUFFIXES


def _is_scanner_itself(text: str) -> bool:
    """按**内容标记**认出扫描器自身(含被改名的副本)。

    preflight 把本文件复制成 `_scanner.py` 搬进临时目录再跑 —— 按文件名排除
    在那里失效,扫描器就会扫到自己那一整套校准样本,满仓红。
    """
    return SELF_EXCLUSION_MARKER in text


def _generic_checks_enabled(relative: str, text: str = "") -> bool:
    if relative == "scripts/scan_repository_secrets.py":
        return False
    if text and _is_scanner_itself(text):
        return False
    # 被测物就是凭据本身 —— 与扫描器排除自己同一个理由,不是目录豁免。
    if relative in SECRET_FIXTURE_PATHS:
        return False
    return not relative.startswith(GENERIC_SKIP_PREFIXES)


def scan_text(
    *,
    relative: str,
    text: str,
    known_secrets: Iterable[KnownSecret] = KNOWN_SECRETS,
) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(
        _scan_known_values(
            relative=relative,
            text=text,
            known_secrets=known_secrets,
        )
    )

    if not _generic_checks_enabled(relative, text):
        return findings

    if relative not in PRIVATE_KEY_FIXTURE_PATHS:
        for match in PRIVATE_KEY_RE.finditer(text):
            findings.append(
                Finding(relative, _line_for_offset(text, match.start()), "PRIVATE_KEY")
            )
    for match in CREDENTIAL_URL_RE.finditer(text):
        # [WO_245] 模板化的 `user:pass@` 不是泄露。判**两段**:
        # 口令段是占位写法(`<PAT>` / `ghp_xxxx`),或用户名段本身是模板
        # (`https://用户名:Token@...` —— 非 ASCII 的 userinfo 不可能是真账号),
        # 都放行;`root:password@` 这种两段都像真值的照旧红。
        credential = _CREDENTIAL_URL_VALUE_RE.search(
            text[match.start() : match.start() + 400]
        )
        if credential and (
            _looks_like_placeholder(credential.group("value"))
            or _looks_like_placeholder(credential.group("user"))
            or not credential.group("user").isascii()
            # [WO_245 三轮] 指向 RFC 保留域的凭据不可能是真的。
            or _RESERVED_HOST_RE.match(credential.group("host").split(":")[0])
            # RFC 3986 的标准占位对,两段同时是模板词。
            or (
                credential.group("user").lower(),
                credential.group("value").lower(),
            ) in _TEMPLATE_CREDENTIAL_PAIRS
        ):
            continue
        findings.append(
            Finding(relative, _line_for_offset(text, match.start()), "CREDENTIAL_URL")
        )
    if _behavioural_checks_enabled(relative):
        for match in AUTO_ADD_POLICY_RE.finditer(text):
            findings.append(
                Finding(relative, _line_for_offset(text, match.start()), "SSH_AUTO_ADD_POLICY")
            )
    for line, name, value in _env_default_hits(relative, text):
        if _env_default_is_benign(value):
            continue
        findings.append(Finding(relative, line, f"ENV_DEFAULT_SECRET:{name}"))

    # [WO_245 订正] 凭据**形态**。这是材料类规则 —— 在 .md 里与在 .py 里后果相同,
    # 不进 _BEHAVIOURAL_CODES。
    for label, regex in _TOKEN_SHAPE_RULES:
        for match in regex.finditer(text):
            value = match.group(0)
            if _looks_like_placeholder(value) or _is_low_entropy_filler(value):
                continue
            findings.append(
                Finding(
                    relative,
                    _line_for_offset(text, match.start()),
                    f"TOKEN_SHAPE:{label}",
                )
            )

    if PARAMIKO_IMPORT_RE.search(text):
        for match in PLAINTEXT_PASSWORD_KWARG_RE.finditer(text):
            if _looks_like_placeholder(match.group("value")):
                continue
            findings.append(
                Finding(
                    relative,
                    _line_for_offset(text, match.start()),
                    "PARAMIKO_PLAINTEXT_PASSWORD",
                )
            )
    for regex in (HARDCODED_ASSIGNMENT_RE, UPPERCASE_TOKEN_ASSIGNMENT_RE):
        for match in regex.finditer(text):
            if (
                not _looks_like_non_secret_name(match.group("name"))
                and not _looks_like_placeholder(match.group("value"))
            ):
                findings.append(
                    Finding(
                        relative,
                        # 🔴 用 name 的位置,不用 match.start()。
                        #    开头的 `^\s*` 会把**前面的空行**一起吞进 match,
                        #    match.start() 因此落在空行上 —— 位置清单会指错行。
                        _line_for_offset(text, match.start("name")),
                        f"HARDCODED_CREDENTIAL:{match.group('name')}",
                    )
                )
    return findings


def _scan_known_values(
    *,
    relative: str,
    text: str,
    known_secrets: Iterable[KnownSecret],
) -> list[Finding]:
    findings: list[Finding] = []
    known_by_length: dict[int, dict[str, str]] = {}
    for secret in known_secrets:
        known_by_length.setdefault(secret.length, {})[secret.sha256] = secret.label

    for value, offset in _candidate_values(text):
        labels = known_by_length.get(len(value))
        if not labels:
            continue
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        label = labels.get(digest)
        if label:
            findings.append(
                Finding(relative, _line_for_offset(text, offset), f"KNOWN_SECRET:{label}")
            )
    return findings


def _tracked_paths(root: Path) -> Iterator[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    for raw in result.stdout.split(b"\0"):
        if raw:
            path = root / raw.decode("utf-8", errors="strict")
            if path.is_file():
                yield path


def _image_paths(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in IGNORED_DIR_NAMES for part in relative_parts):
            continue
        yield path


def scan_root(
    root: Path,
    mode: str,
    apply_register: bool = True,
    stats: dict[str, int] | None = None,
    scanned_out: set[str] | None = None,
) -> list[Finding]:
    root = root.resolve()
    paths = _tracked_paths(root) if mode == "tracked" else _image_paths(root)
    findings: list[Finding] = []
    scanned: set[str] = set()
    for path in paths:
        scanned.add(_normalise_relative(path, root))
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append(
                Finding(_normalise_relative(path, root), 1, f"READ_ERROR:{type(exc).__name__}")
            )
            continue
        if _is_binary(data):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(
            scan_text(relative=_normalise_relative(path, root), text=text)
        )
    if scanned_out is not None:
        scanned_out.update(scanned)
    if apply_register:
        findings = apply_transitional_register(
            findings, scanned=scanned, stats=stats
        )
    elif stats is not None:
        stats.setdefault("suppressed_transitional", 0)
        stats.setdefault("suppressed_fixtures", 0)
        stats.setdefault("out_of_scope", 0)
    return sorted(findings, key=lambda item: (item.path, item.line, item.code))


def apply_transitional_register(
    findings: list[Finding],
    register: Iterable[TransitionalExposure] | None = None,
    scanned: set[str] | None = None,
    stats: dict[str, int] | None = None,
) -> list[Finding]:
    """放行登记在册的存量暴露与判据夹具;**数目对不上就红**,两个方向都红。

    `scanned` = 本次真的扫过的相对路径集合。给了它,陈账检查只对
    **在扫描范围内**的条目做 —— 不在范围的既没有读数,就不能说它「已拆除」。

    🔴 这不是把陈账检查放松了。preflight 只把本包变更文件搬进临时目录再扫,
       登记表里其余文件根本不在场;拿「不在场」当「已拆除」去报 STALE,
       是把**没有读数**误读成**读数为 0** —— 同一个病的另一张脸
       (参见 a-gate-that-cannot-say-it-did-not-run)。
       整树扫时每个条目都在范围内,语义与从前完全一致。
    """
    if register is None:
        register = TRANSITIONAL_EXPOSURES + FIXTURE_CREDENTIALS
    register = tuple(register)
    counted: dict[tuple[str, str], int] = {}
    for finding in findings:
        key = (finding.path, finding.code)
        counted[key] = counted.get(key, 0) + 1

    kept: list[Finding] = []
    quota = {(entry.path, entry.code): entry.count for entry in register}
    used: dict[tuple[str, str], int] = {}
    for finding in findings:
        key = (finding.path, finding.code)
        if key in quota and used.get(key, 0) < quota[key]:
            used[key] = used.get(key, 0) + 1
            continue
        kept.append(finding)

    out_of_scope = 0
    for entry in register:
        key = (entry.path, entry.code)
        if scanned is not None and entry.path not in scanned:
            out_of_scope += 1
            continue
        actual = counted.get(key, 0)
        if actual < entry.count:
            # 已经拆掉了(或位置变了),登记条目成了陈账 —— 必须同班删除。
            kept.append(
                Finding(
                    entry.path,
                    1,
                    f"STALE_TRANSITIONAL_REGISTER:{entry.code}"
                    f"(登记 {entry.count} 实测 {actual};{entry.tracking})",
                )
            )

    if stats is not None:
        transitional_keys = {(e.path, e.code) for e in TRANSITIONAL_EXPOSURES}
        stats["suppressed_transitional"] = sum(
            n for k, n in used.items() if k in transitional_keys
        )
        stats["suppressed_fixtures"] = sum(
            n for k, n in used.items() if k not in transitional_keys
        )
        stats["out_of_scope"] = out_of_scope
    return kept


def scan_history(root: Path) -> list[Finding]:
    """Scan every reachable Git blob for the known compromised values."""

    root = root.resolve()
    rev_list = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--objects", "--all"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    object_paths: dict[str, str] = {}
    for line in rev_list.stdout.splitlines():
        if not line:
            continue
        oid, _, path = line.partition(" ")
        object_paths.setdefault(oid, path or "<unmapped>")

    checks = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch-check"],
        input="\n".join(object_paths) + "\n",
        check=True,
        capture_output=True,
        text=True,
        encoding="ascii",
        errors="replace",
    )
    blob_oids: list[str] = []
    for line in checks.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "blob":
            blob_oids.append(parts[0])

    process = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    findings: list[Finding] = []
    try:
        for oid in blob_oids:
            process.stdin.write(oid.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii", errors="replace").strip()
            parts = header.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue
            size = int(parts[2])
            data = process.stdout.read(size)
            process.stdout.read(1)
            if _is_binary(data):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            relative = f"history/{oid}/{object_paths.get(oid, '<unmapped>')}"
            findings.extend(
                _scan_known_values(
                    relative=relative,
                    text=text,
                    known_secrets=KNOWN_SECRETS,
                )
            )
    finally:
        process.stdin.close()
        process.wait(timeout=30)
    return sorted(findings, key=lambda item: (item.path, item.line, item.code))


def _register_totals() -> tuple[int, int, int]:
    """(待拆除的命中数, 判据夹具的命中数, 还没拿到单号的条数)。

    🔴 两个数分开报。把「等着被拆的真暴露」和「永远会留着的判据夹具」
       加成一个数,就又制造了一次「出声但没说对」。
    """
    transitional = sum(e.count for e in TRANSITIONAL_EXPOSURES)
    fixtures = sum(e.count for e in FIXTURE_CREDENTIALS)
    pending = sum(1 for e in TRANSITIONAL_EXPOSURES if e.tracking.startswith("待定:"))
    return transitional, fixtures, pending


def _print_report(root: Path, mode: str) -> None:
    """--report:按规则名列命中位置。**不打印值。**

    给复审做反向对照用 —— 「闸口绿」和「什么都没有」在 pass 行上分不开,
    这个子模式让两者分得开。
    """
    findings = scan_root(root, mode, apply_register=False)
    by_code: dict[str, list[Finding]] = {}
    for finding in findings:
        by_code.setdefault(finding.code.split(":")[0], []).append(finding)

    registered = {
        (e.path, e.code): e
        for e in TRANSITIONAL_EXPOSURES + FIXTURE_CREDENTIALS
    }
    print(f"secret_scan_report mode={mode} rules={len(by_code)} findings={len(findings)}")
    for code in sorted(by_code):
        group = by_code[code]
        print(f"\n[{code}] {len(group)} 处")
        for finding in group:
            entry = registered.get((finding.path, finding.code))
            mark = f"  ← 已登记({entry.tracking})" if entry else ""
            print(f"    {finding.path}:{finding.line}: {finding.code}{mark}")

    transitional, fixtures, pending = _register_totals()
    print(
        f"\nregister entries="
        f"{len(TRANSITIONAL_EXPOSURES) + len(FIXTURE_CREDENTIALS)} "
        f"transitional={transitional} fixtures={fixtures} pending_tracking={pending}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("tracked", "image", "history"), required=True)
    parser.add_argument(
        "--report",
        action="store_true",
        help="按规则名列出命中位置(不打印值),不做闸口判定",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="公开快照门:过渡登记表**不放行**,要求 transitional=0 且 pending=0",
    )
    args = parser.parse_args()

    if args.report:
        if args.mode == "history":
            parser.error("--report 不支持 history 模式")
        _print_report(args.root, args.mode)
        return 0

    # 🔴 [WO_261 ④ · 公开门] 私仓的**过渡豁免**不能随包变成公开快照的放行依据。
    #    私仓里「这 10 处 PAT 已登记、等 WO_247 清」是一条**内部约定**;
    #    快照一旦公开,那 10 处就是 10 处明文凭据 —— 约定不跟着走。
    #    ⇒ --public 只保留 FIXTURE_CREDENTIALS(合成夹具,逐文件精确计数),
    #      TRANSITIONAL_EXPOSURES **一条都不放行**。
    #    ⚠️ 私仓默认模式的行为**一个字没改**(Deploy 现役 preflight 5-b 在用)。
    public_register = FIXTURE_CREDENTIALS if args.public else None

    stats: dict[str, int] = {}
    if args.mode == "history":
        findings = scan_history(args.root)
    elif args.public:
        # 🔴 scanned 必须是「**扫过的**文件集」,不是「有命中的文件集」——
        #    拿后者当范围,陈账检查会把绝大多数登记条目当成「不在范围」跳过,
        #    于是一条已经消失的夹具登记永远不会被报出来(又一次「没读数当零读数」)。
        scanned_rel: set[str] = set()
        findings = scan_root(
            args.root, args.mode, apply_register=False,
            stats=stats, scanned_out=scanned_rel,
        )
        findings = apply_transitional_register(
            findings, register=public_register, scanned=scanned_rel, stats=stats
        )
        findings = sorted(findings, key=lambda i: (i.path, i.line, i.code))
    else:
        findings = scan_root(args.root, args.mode, stats=stats)
    if findings:
        for finding in findings[:100]:
            print(f"{finding.path}:{finding.line}: {finding.code}", file=sys.stderr)
        if len(findings) > 100:
            print(
                f"... {len(findings) - 100} additional findings omitted",
                file=sys.stderr,
            )
        print(f"secret_scan=failed findings={len(findings)}", file=sys.stderr)
        return 1

    # 🔴 [WO_245 三轮] 「出声 ≠ 说对了说的是什么」。
    #    过去这里只印一行 secret_scan=passed —— 而仓里其实有 13 处命中被登记表放行。
    #    那行绿把「仓里干净」和「仓里有东西但我们同意先放着」说成了同一件事。
    #    ⇒ 登记数必须出现在这一行;n>0 时带 ⚠,不许读起来像纯绿。
    #    🔴 报的是**本次真的被放行的条数**,不是登记表的声明总数。
    #       preflight 只把本包变更文件搬进临时目录扫,登记表里其余文件不在场;
    #       那时印「transitional=13」就又一次「出声但没说对」——
    #       13 是账面数,本次实际放行可能是 0。
    _declared_t, _declared_f, declared_pending = _register_totals()
    transitional = stats.get("suppressed_transitional", 0)
    fixtures = stats.get("suppressed_fixtures", 0)
    out_of_scope = stats.get("out_of_scope", 0)
    # 公开门下过渡登记**一条都没参与**,所以 pending 也不是 0 以外的任何数;
    # 报 declared_pending 会把一个**没参与本次判定**的内部计数写进公开门读数。
    pending = 0 if args.public else declared_pending
    if args.public:
        # 走到这里说明 findings 为空 ⇒ 没有任何 transitional 命中漏出来。
        # 这两条断言是**自证**,不是装饰:公开门的硬条件就是这两个 0,
        # 如果它们不成立却还印了 passed,这行绿就是假的。
        assert transitional == 0, "公开门:过渡登记不参与放行,transitional 必须为 0"
        assert pending == 0
    marker = "⚠ " if transitional else ""
    print(
        f"{marker}secret_scan=passed mode={args.mode} "
        f"transitional={transitional} fixtures={fixtures} "
        f"pending_tracking={pending} register_out_of_scope={out_of_scope}"
    )
    if transitional:
        print(
            f"{marker}注意:上面的 passed 不代表仓里没有凭据材料 —— "
            f"本次有 {transitional} 处**待拆除**的命中由过渡登记表放行"
            f"(另有 {fixtures} 处判据夹具常驻豁免),"
            f"逐条位置见 --report;其中 {pending} 条还没有拆除单号。"
        )
    if out_of_scope:
        print(
            f"说明:登记表里有 {out_of_scope} 条的文件不在本次扫描范围内,"
            f"对它们**没有读数**,因此不判陈账(不是「已拆除」)。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
