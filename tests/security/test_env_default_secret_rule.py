"""WO_245 · 环境变量兜底字面量规则 + docs/ 开闸 的判据。

三条反向对照按工单要求走**真子进程 + 真 git 仓**,不是只调 scan_text:
闸口是否真的会红,取决于 tracked 模式的枚举、docs/ 前缀、以及 main() 的退出码,
只测纯函数证明不了这条链是通的。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import scan_repository_secrets as secret_scanner
from scripts.scan_repository_secrets import (
    Finding,
    TransitionalExposure,
    apply_transitional_register,
    scan_text,
)

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "scan_repository_secrets.py"

# 人造载荷:形状与 server.py:6766 同,值是本文件自造的,不是任何真口令。
INJECTED_PY = 'value = os.environ.get("X_PASSWORD", "abc12345")\n'
INJECTED_MD = (
    "# 临时文档\n\n"
    '    value = os.environ.get("X_PASSWORD", "abc12345")\n'
)


def _codes(relative: str, text: str) -> set[str]:
    return {finding.code for finding in scan_text(relative=relative, text=text)}


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)


def _commit_all(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=WO245 Gate Test",
            "-c", "user.email=wo245@example.invalid",
            "commit", "-m", "fixture",
        ],
        check=True,
        capture_output=True,
    )


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo), "--mode", "tracked"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# --------------------------------------------------------------- 正样本臂 ---


def test_injected_py_default_literal_turns_the_gate_red(tmp_path: Path) -> None:
    """反向对照 ①:临时 .py 注入必红。"""
    repo = tmp_path / "py_repo"
    _init_repo(repo)
    (repo / "handler.py").write_text(INJECTED_PY, encoding="utf-8")
    _commit_all(repo)

    result = _run_gate(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ENV_DEFAULT_SECRET:X_PASSWORD" in result.stderr
    assert "handler.py" in result.stderr


def test_injected_docs_markdown_turns_the_gate_red(tmp_path: Path) -> None:
    """反向对照 ②:docs/ 下的 .md 注入必红。

    这一臂守的是「docs/ 不再整树跳过」——
    若有人把 "docs/" 加回 GENERIC_SKIP_PREFIXES，它必须当场红。
    """
    repo = tmp_path / "docs_repo"
    _init_repo(repo)
    doc = repo / "docs" / "operations" / "note.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(INJECTED_MD, encoding="utf-8")
    _commit_all(repo)

    result = _run_gate(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ENV_DEFAULT_SECRET:X_PASSWORD" in result.stderr
    assert "docs/operations/note.md" in result.stderr


def test_docs_prefix_is_not_in_the_generic_skip_list() -> None:
    assert "docs/" not in secret_scanner.GENERIC_SKIP_PREFIXES


# --------------------------------------------------------------- 反样本臂 ---


def test_removing_the_rule_makes_the_same_payload_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向对照 ③:去掉规则后同一载荷必绿。

    这一臂证的是「前两臂的红是**这条规则**发出的」——
    不是 KNOWN_SECRET、不是 HARDCODED_ASSIGNMENT、也不是别的什么顺手接住了。
    没有它，前两臂的绿读不出归因。
    """
    monkeypatch.setattr(
        secret_scanner, "_env_default_hits", lambda relative, text: []
    )
    assert _codes("handler.py", INJECTED_PY) == set()
    assert _codes("docs/operations/note.md", INJECTED_MD) == set()


def test_rule_is_live_without_the_patch() -> None:
    """与上一条成对:不打补丁时同一载荷必须是红的。"""
    assert "ENV_DEFAULT_SECRET:X_PASSWORD" in _codes("handler.py", INJECTED_PY)
    assert "ENV_DEFAULT_SECRET:X_PASSWORD" in _codes(
        "docs/operations/note.md", INJECTED_MD
    )


# ------------------------------------------------------------- 被调方判据 ---


def test_a_dict_get_is_not_an_environment_read() -> None:
    """名字里带 TOKEN 不是判据，**被调方**才是。"""
    text = 'n = usage.get("completion_tokens", "fallback-value")\n'
    assert not {c for c in _codes("scripts/report.py", text) if c.startswith("ENV_DEFAULT")}


def test_nested_environ_get_default_is_unwrapped() -> None:
    text = (
        'v = os.environ.get("A_PASSWORD", os.environ.get("B_PASSWORD", "s3cr3tval"))\n'
    )
    codes = _codes("services/x.py", text)
    assert "ENV_DEFAULT_SECRET:B_PASSWORD" in codes


def test_or_chain_fallback_literal_is_caught() -> None:
    text = 'salt = os.getenv("A_SALT") or os.getenv("B_SALT") or "literal-salt-x"\n'
    assert "ENV_DEFAULT_SECRET:A_SALT" in _codes("services/x.py", text)


def test_unparseable_python_falls_back_to_the_text_arm() -> None:
    """解析失败不得静默关掉这条规则。"""
    text = INJECTED_PY + "def broken(:\n"
    assert secret_scanner._env_default_hits_ast(text) is None
    assert "ENV_DEFAULT_SECRET:X_PASSWORD" in _codes("services/broken.py", text)


# ----------------------------------------------------- 按规则的豁免 ---


def test_flag_literal_defaults_are_exempt_by_value_shape() -> None:
    """开关默认值不是凭据——按**值**豁免，不按名字也不按目录。"""
    for literal in ("false", "true", "0", "1", "off"):
        text = f'flag = os.getenv("TV_ACCESS_PASSWORD_LEGACY_ENABLED", "{literal}")\n'
        codes = {c for c in _codes("api/x.py", text) if c.startswith("ENV_DEFAULT")}
        assert not codes, f"{literal} should be exempt, got {codes}"


def test_endpoint_literal_defaults_are_exempt() -> None:
    text = 'u = os.getenv("MHZ_SVIDEO_STS_TOKEN_URL", "https://example.invalid/sts")\n'
    assert not {c for c in _codes("services/x.py", text) if c.startswith("ENV_DEFAULT")}


def test_a_credentialled_endpoint_default_is_still_caught() -> None:
    """端点豁免不得顺手放过带凭据的 URL。

    🔴 主机用 omnirank.top 而不是 example.invalid:后者是 RFC 保留域,
       三轮起被保留域规则豁免 —— 正样本臂不能挑一个「按规则就该放行」的主机,
       否则这一臂会变成空转的绿。
    """
    text = 'u = os.getenv("X_TOKEN_URL", "https://svc:A7f9Qm2Lp4Xz@omnirank.top/x")\n'
    assert "CREDENTIAL_URL" in _codes("services/x.py", text)


def test_placeholder_credential_urls_are_exempt_but_real_ones_are_not() -> None:
    assert "CREDENTIAL_URL" not in _codes(
        "docs/guide.md", "git push https://user:<PAT>@github.com/x/y.git\n"
    )
    assert "CREDENTIAL_URL" not in _codes(
        "docs/guide.md", "git clone https://user:ghp_xxxx@gitee.com/x/y.git\n"
    )
    # 非 ASCII 的 userinfo = 模板（「用户名」），不是真账号。
    assert "CREDENTIAL_URL" not in _codes(
        "docs/guide.md", "git clone https://用户名:Token@gitee.com/x/y.git\n"
    )
    assert "CREDENTIAL_URL" in _codes(
        "docs/guide.md", "git push https://user:A7f9Qm2Lp4Xz@github.com/x/y.git\n"
    )
    # 🔴 裸词口令是**真弱口令**，不得当占位符放过。
    #    主机改用真域名:example.invalid 是 RFC 保留域，三轮起按规则豁免。
    assert "CREDENTIAL_URL" in _codes(
        "services/x.py", 'URL = "https://root:password@omnirank.top/path"\n'
    )


def test_behavioural_rule_applies_to_code_but_not_to_prose() -> None:
    """AutoAddPolicy 描述的是**代码会做什么**，散文引用它不会执行。"""
    snippet = "ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())\n"
    assert "SSH_AUTO_ADD_POLICY" in _codes("scripts/deploy.py", snippet)
    assert "SSH_AUTO_ADD_POLICY" not in _codes("docs/AI-CONTEXT/postmortem.md", snippet)


def test_material_rules_still_apply_inside_docs() -> None:
    """凭据**材料**类规则在 docs 里同样成立——别把豁免扩大到整棵树。"""
    assert "PRIVATE_KEY" in _codes(
        "docs/x.md", "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    )
    assert "HARDCODED_CREDENTIAL:DB_PASSWORD" in _codes(
        "docs/x.md", 'DB_PASSWORD = "Qw8vN2pL5xTz"\n'
    )


# --------------------------------------------------- 过渡登记表的锁 ---


def test_register_lets_through_exactly_the_registered_count() -> None:
    register = (
        TransitionalExposure(
            path="server.py", code="ENV_DEFAULT_SECRET:X", count=1,
            reason="r", tracking="WO_246",
        ),
    )
    two = [Finding("server.py", 10, "ENV_DEFAULT_SECRET:X"),
           Finding("server.py", 99, "ENV_DEFAULT_SECRET:X")]
    kept = apply_transitional_register(two, register=register)
    assert [f.line for f in kept] == [99], "超出登记数的那一条必须漏出来"


def test_register_goes_red_when_the_exposure_is_gone() -> None:
    """拆完了但登记没删 ⇒ 红。否则它就变成没人回头看的既成事实。"""
    register = (
        TransitionalExposure(
            path="server.py", code="ENV_DEFAULT_SECRET:X", count=1,
            reason="r", tracking="WO_246",
        ),
    )
    kept = apply_transitional_register([], register=register)
    assert len(kept) == 1
    assert kept[0].code.startswith("STALE_TRANSITIONAL_REGISTER:")


def test_every_registered_entry_is_currently_real_and_tracked() -> None:
    """登记表不得有陈账，也不得有空话的 tracking。"""
    for entry in secret_scanner.TRANSITIONAL_EXPOSURES:
        source = (ROOT / entry.path).read_text(encoding="utf-8")
        actual = [
            f for f in scan_text(relative=entry.path, text=source)
            if f.code == entry.code
        ]
        assert len(actual) == entry.count, (
            f"{entry.path} {entry.code}: 登记 {entry.count} 实测 {len(actual)}"
        )
        assert entry.reason.strip()
        assert "WO_" in entry.tracking or entry.tracking.startswith("待定:")


def test_pending_tracking_numbers_are_counted_and_must_shrink_to_zero() -> None:
    """「待定」不得成为永久状态。

    Review 给出单号后，填进去的同时这个数必须降——
    两边对不上就红。
    """
    pending = [
        e for e in secret_scanner.TRANSITIONAL_EXPOSURES
        if e.tracking.startswith("待定:")
    ]
    assert len(pending) == secret_scanner.PENDING_TRACKING_COUNT, (
        f"待定条目实测 {len(pending)}，"
        f"PENDING_TRACKING_COUNT 写的是 {secret_scanner.PENDING_TRACKING_COUNT}"
    )


def test_tracked_tree_is_green_only_because_of_the_register() -> None:
    """闸口绿不等于仓里没东西 —— 把登记表拿掉必须红。

    [WO_247 改] 原版遍历真实 TRANSITIONAL_EXPOSURES;清干净后它恒空,
    `assert raw` 会红在前提上而不是性质上。改用合成样本,性质不变:
    **同一批原始读数,有登记则被放行、无登记则原样留下。**
    """
    raw = [f for f in scan_text(relative=_SYNTH_DOC,
                                text="token = " + _FAKE_CLASSIC_PAT + "\n")
           if f.code == "TOKEN_SHAPE:GITHUB_PAT"]
    assert raw, "合成样本必须先被规则抓到,否则下面两句没有分辨力"

    entry = _synth_entry(count=len(raw))
    assert apply_transitional_register(
        list(raw), register=(entry,), scanned={_SYNTH_DOC}) == []
    assert apply_transitional_register(list(raw), register=()) == raw

# ------------------------------------------- WO_245 订正 · 凭据形态规则 ---

# 人造载荷:形状合规但值是本文件造的,不是任何真凭据。
# 🔴 长度就地断言 —— 夹具短一位就会让整条规则「测不到」而判据照样绿,
#    而那种绿与「规则生效了」读起来一模一样。
_ALPHABET = "aB3dE7fG1hJ4kL9mN2pQ6rS8tU0vW5xY3zA7bC4iK6oR2uV8wX1yZ5"


def _fake_body(length: int) -> str:
    body = (_ALPHABET * (length // len(_ALPHABET) + 1))[:length]
    assert len(body) == length
    assert len(set(body)) > 6, "别造成低熵填充，会被豁免规则吃掉"
    return body


_FAKE_CLASSIC_PAT = "ghp_" + _fake_body(36)
_FAKE_FINE_PAT = "github_pat_" + _fake_body(60)
_FAKE_AWS_KEY = "AKIA" + "Q7ZP3LMR9XT2VKDN"
_FAKE_OPENAI_KEY = "sk-" + _fake_body(24)

assert len(_FAKE_CLASSIC_PAT) == 40
assert len(_FAKE_AWS_KEY) == 20


def test_token_shapes_are_caught_in_code_and_in_docs() -> None:
    """凭据**材料**类规则在 .md 与 .py 里后果相同,两边都要红。"""
    for payload, label in (
        (_FAKE_CLASSIC_PAT, "GITHUB_PAT"),
        (_FAKE_FINE_PAT, "GITHUB_PAT"),
        (_FAKE_AWS_KEY, "AWS_ACCESS_KEY"),
        (_FAKE_OPENAI_KEY, "OPENAI_KEY"),
    ):
        text = f"token = {payload}\n"
        assert f"TOKEN_SHAPE:{label}" in _codes("services/x.py", text), payload[:8]
        assert f"TOKEN_SHAPE:{label}" in _codes("docs/note.md", text), payload[:8]


def test_masked_and_filler_token_shapes_are_exempt() -> None:
    """打码写法与低熵填充样例不是凭据 —— 按**值的形状**豁免。"""
    assert not _codes("docs/note.md", "use ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
    assert not _codes("docs/note.md", "key = sk-AAAAAAAAAAAAAAAAAAAAAAAA\n")


def test_paramiko_plaintext_password_is_caught_but_redacted_one_is_not() -> None:
    live = (
        "import paramiko\n"
        "ssh.connect('10.0.0.1', 22, username='root', password='Qw8vN2pL5xTz')\n"
    )
    assert "PARAMIKO_PLAINTEXT_PASSWORD" in _codes("scripts/deploy.py", live)

    redacted = (
        "import paramiko\n"
        "ssh.connect('10.0.0.1', 22, username='root', password='<redacted-2026>')\n"
    )
    assert "PARAMIKO_PLAINTEXT_PASSWORD" not in _codes("docs/handoff.md", redacted)

    # 没 import paramiko 就不是这条规则的事(别把所有 password= 都揽过来)。
    assert "PARAMIKO_PLAINTEXT_PASSWORD" not in _codes(
        "scripts/other.py", "ssh.connect(password='Qw8vN2pL5xTz')\n"
    )


def test_token_shape_rule_fires_on_docs_and_the_tree_is_now_clean() -> None:
    """[WO_247 改名+改判据] 原臂叫 `..._still_contains_the_registered_github_pats`,
    断言的是「那 10 处**还在**」—— 属「仍然存在」型锁,随 WO_247 的修法同班退役。

    它守的性质仍然有效,拆成两条:
      ① 规则真的会在 docs/ 路径上响(有人摘掉 TOKEN_SHAPE 规则、
         或把 docs/ 重新跳过时当场红)—— 用合成样本,不依赖仓里有暴露;
      ② **棘轮**:真实工作树上 GitHub PAT 形状命中必须为 0。
         (夹具文件本身除外 —— 它是「低熵填充须被豁免」那条判据的靶子。)
    """
    hits = [f for f in scan_text(relative=_SYNTH_DOC,
                                 text="token = " + _FAKE_CLASSIC_PAT + "\n")
            if f.code == "TOKEN_SHAPE:GITHUB_PAT"]
    assert len(hits) == 1, "规则没有在 docs/ 路径上响"

    import subprocess as _sp
    out = _sp.run(
        ["git", "grep", "-hoE",
         r"gh[po]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82}",
         "--", ":(exclude)tests/security/test_env_default_secret_rule.py"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    found = [x for x in out.stdout.split() if x]
    assert found == [], (
        "工作树上又出现了 GitHub PAT 形状(WO_247 清理后应恒为 0):"
        + ", ".join(x[:8] + "..." for x in found))

def test_removing_the_token_shape_rule_makes_the_baseline_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """毒:去掉凭据形态规则后,**同一份样本**必须一条都报不出。

    与上一臂成对 —— 没有它,上一臂的红读不出归因。

    🔴 [WO_247 · 2026-09-22 Deploy 改] 原版遍历真实 `TRANSITIONAL_EXPOSURES`。
       我把**正臂**换成了合成样本 `_SYNTH_DOC`,却把**这条毒臂**留在真表上;
       WO_247 清空登记表后它变成空循环 —— 测试恒绿,**一颗牙都没有**。
       毒证:把 `assert False` 下在循环体内(缩进 8 > for 的 4),仍 `1 passed`。
       改法:毒臂与正臂**喂同一份输入、走同一条路径**,只摘掉规则。
       一对判据要一起改 —— 只改一半会让其中一臂静默退役。
    """
    monkeypatch.setattr(secret_scanner, "_TOKEN_SHAPE_RULES", ())
    hits = [f for f in scan_text(relative=_SYNTH_DOC,
                                 text="token = " + _FAKE_CLASSIC_PAT + "\n")
            if f.code.startswith("TOKEN_SHAPE:")]
    assert hits == [], (
        "摘掉 TOKEN_SHAPE 规则后仍报出形态命中 ⇒ 上一臂的红读不出归因:"
        + repr([f.code for f in hits]))


# =================================================== WO_245 第三轮 ==========
# ② 闸口必须说出登记数 · ③ tests/ 开闸 · ④ a4 补报的形态

_FAKE_GOOGLE_KEY = "AIza" + _fake_body(35)
_FAKE_SLACK_TOKEN = "xoxb-" + "1234567890-9876543210-" + _fake_body(24)


def test_pass_line_states_the_register_counts_when_nonzero(tmp_path: Path) -> None:
    """🔴 「出声 ≠ 说对了说的是什么」。

    登记非空时,pass 行必须把 transitional / fixtures / pending 数说出来并带 ⚠,
    否则「仓里干净」和「仓里有东西但同意先放着」读起来一模一样。

    [WO_247 改] 改用合成登记表 —— 真实登记表清零后本臂不该跟着失效。
    """
    work = _work_with_synth_hit(tmp_path)
    _scanner_with_register(work, "    " + repr_synth_entry() + ",\n")
    result = _run_gate_with(work, mode="image")
    assert result.returncode == 0, result.stderr
    assert "transitional=1" in result.stdout, result.stdout
    assert "pending_tracking=" in result.stdout
    assert "\u26a0" in result.stdout, "登记非空且真放行时 pass 行必须带 ⚠"
    assert "passed" in result.stdout

def test_report_mode_lists_positions_by_rule_without_values(tmp_path: Path) -> None:
    """--report:按规则名列位置,且**不打印值**。"""
    repo = tmp_path / "report_repo"
    _init_repo(repo)
    (repo / "handler.py").write_text(
        f'value = os.environ.get("X_PASSWORD", "abc12345")\ntok = "{_FAKE_CLASSIC_PAT}"\n',
        encoding="utf-8",
    )
    _commit_all(repo)

    result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo),
         "--mode", "tracked", "--report"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert "secret_scan_report" in result.stdout
    assert "[ENV_DEFAULT_SECRET]" in result.stdout
    assert "[TOKEN_SHAPE]" in result.stdout
    assert "handler.py:1" in result.stdout
    assert "handler.py:2" in result.stdout
    # 🔴 值不许出现在报告里。
    assert "abc12345" not in result.stdout
    assert _FAKE_CLASSIC_PAT not in result.stdout


def test_report_mode_shows_pre_register_findings(tmp_path: Path) -> None:
    """--report 报的是**登记前**的原始命中 —— 否则它就复制了 pass 行的盲点。

    [WO_247 改] 用合成登记表 + 合成命中,不再依赖仓里真有暴露。
    """
    work = _work_with_synth_hit(tmp_path)
    _scanner_with_register(work, "    " + repr_synth_entry() + ",\n")
    result = _run_gate_with(work, mode="image", report=True)
    assert result.returncode == 0, result.stderr
    assert "TOKEN_SHAPE:GITHUB_PAT" in result.stdout, result.stdout
    assert "\u5df2\u767b\u8bb0" in result.stdout, result.stdout

# ------------------------------------------------- a4 补报的三条反向对照 ---


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('AWS_ACCESS_KEY_ID = "AKIAQ7ZP3LMR9XT2VKDN"\n',
         "HARDCODED_CREDENTIAL:AWS_ACCESS_KEY_ID"),
        (f'GH_TOKEN = "{_fake_body(32)}"\n', "HARDCODED_CREDENTIAL:GH_TOKEN"),
        (f'x = "{_FAKE_CLASSIC_PAT}"\n', "TOKEN_SHAPE:GITHUB_PAT"),
    ],
)
def test_a4_reported_gaps_are_now_red(payload: str, expected: str) -> None:
    """a4 实测旧闸这三者 0 命中。"""
    assert expected in _codes("api/x.py", payload), payload[:24]


def test_google_and_slack_token_shapes_are_caught() -> None:
    assert "TOKEN_SHAPE:GOOGLE_API_KEY" in _codes(
        "api/x.py", f'k = "{_FAKE_GOOGLE_KEY}"\n')
    assert "TOKEN_SHAPE:SLACK_TOKEN" in _codes(
        "api/x.py", f'k = "{_FAKE_SLACK_TOKEN}"\n')


def test_bare_lowercase_token_is_not_a_credential_name() -> None:
    """🔴 裸 TOKEN 只认**大写常量形**。

    实测把裸 TOKEN 并进大小写不敏感那条,全仓 6 → 42,多出的 36 条
    几乎全是测试里的 `token = "..."`(不透明标识符夹具)。
    大小写在这里有信息量:大写常量是配置型凭据,小写局部是标识符。
    """
    assert not {c for c in _codes("tests/x.py", 'token = "abc12345678"\n')
                if c.startswith("HARDCODED_CREDENTIAL")}
    assert "HARDCODED_CREDENTIAL:GH_TOKEN" in _codes(
        "api/x.py", 'GH_TOKEN = "abc12345678"\n')


def test_categoriser_constants_are_not_credentials() -> None:
    """名字带 TOKEN 但指**种类/状态/用途**的枚举常量不是凭据。"""
    for name in ("TOKEN_PURPOSE_CONFIRM", "TOKEN_STATUS_SUBMITTED",
                 "ACTOR_KIND_CUSTOMER_TOKEN", "QUOTE_PREDICATE_TOKEN"):
        assert not {c for c in _codes("services/x.py", f'{name} = "submitted"\n')
                    if c.startswith("HARDCODED_CREDENTIAL")}, name


# --------------------------------------------- tests/ 开闸与两张登记表 ---


def test_tests_prefix_is_not_in_the_generic_skip_list() -> None:
    assert "tests/" not in secret_scanner.GENERIC_SKIP_PREFIXES
    assert "frontend/tests/" not in secret_scanner.GENERIC_SKIP_PREFIXES


def test_reserved_hosts_are_exempt_but_real_hosts_are_not() -> None:
    """RFC 2606/6761 保留域解析不到真实服务 ⇒ 指向它们的凭据不可能是真的。"""
    for host in ("example.com", "example.invalid", "cdn.example",
                 "localhost", "127.0.0.1"):
        assert "CREDENTIAL_URL" not in _codes(
            "services/x.py", f'u = "https://root:A7f9Qm2Lp4Xz@{host}/p"\n'), host
    assert "CREDENTIAL_URL" in _codes(
        "services/x.py", 'u = "https://root:A7f9Qm2Lp4Xz@omnirank.top/p"\n')


def test_template_credential_pair_needs_both_sides() -> None:
    """`user:pass@` 是 RFC 3986 的占位对;`root:password@` 不是。"""
    assert "CREDENTIAL_URL" not in _codes(
        "services/x.py", 'u = "https://user:pass@omnirank.top/p"\n')
    assert "CREDENTIAL_URL" in _codes(
        "services/x.py", 'u = "https://root:password@omnirank.top/p"\n')


def test_every_secret_fixture_path_really_handles_secrets() -> None:
    """SECRET_FIXTURE_PATHS 不得被当成普通的目录豁免用。"""
    for rel in secret_scanner.SECRET_FIXTURE_PATHS:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert any(
            marker in source for marker in secret_scanner.SECRET_FIXTURE_EVIDENCE
        ), f"{rel} 不像「以凭据为被测物」的判据文件"
        assert not secret_scanner._generic_checks_enabled(rel)


def test_fixture_register_entries_are_exact_and_not_stale() -> None:
    """每条按路径登记的夹具都必须(a)现在仍然真的命中、(b)数目精确、
    (c)tracking 写「常驻」、(d)理由里明说**为何按值分不开**。

    🔴 (a) 同时就是「按值分不开」这句话的**机器证明**,不只是行文要求:
       `scan_text` 在产出一条命中之前,已经把全部值形状豁免跑过一遍了
       (占位 / 低熵填充 / 开关值 / 端点 URL / RFC 保留域 / RFC 占位对)。
       所以「这条命中还在」⇔「没有任何值形状规则能豁免它」。
       哪天有人补了一条能吃掉它的值形状规则,这条登记就会变成陈账,
       apply_transitional_register 的 STALE 检查当场红 —— 于是这句理由
       不会烂在注释里:它由闸口自己持续复核。
    """
    for entry in secret_scanner.FIXTURE_CREDENTIALS:
        source = (ROOT / entry.path).read_text(encoding="utf-8")
        hits = [f for f in scan_text(relative=entry.path, text=source)
                if f.code == entry.code]
        assert len(hits) == entry.count, (
            f"{entry.path} {entry.code}: 登记 {entry.count} 实测 {len(hits)}")
        assert entry.tracking.startswith("常驻:")
        assert "按值分不开" in entry.reason, (
            f"{entry.path} {entry.code} 的理由缺「按值分不开:…」一句 —— "
            "按路径登记必须说明为何做不到按值形状豁免"
        )
        _what, _, why = entry.reason.partition("按值分不开")
        assert len(why.strip(":： ")) >= 10, (
            f"{entry.path} {entry.code}:「按值分不开」后面得是个理由,不是空话"
        )


def test_line_numbers_never_point_at_a_blank_line() -> None:
    """🔴 `^\s*` 会把前面的空行一起吞进 match ⇒ 位置清单指错行。

    复审要的是位置清单,指错行的清单不能用。
    """
    from scripts.scan_repository_secrets import scan_root
    for finding in scan_root(ROOT, "tracked", apply_register=False):
        lines = (ROOT / finding.path).read_text(encoding="utf-8").splitlines()
        if finding.line - 1 < len(lines):
            assert lines[finding.line - 1].strip(), (
                f"{finding.path}:{finding.line} 指向空行")


# ============================== WO_245 四轮 · preflight 临时目录场景 =========
# preflight 第 5 关把本包变更文件搬进临时目录,并把扫描器复制成 _scanner.py
# 放同目录跑 --mode image。两件事在那里与整树扫不同:
#   · 扫描器是**另一个文件名**的副本 ⇒ 按文件名自排除失效;
#   · 登记表里绝大多数文件**不在场** ⇒ 拿「不在场」当「已拆除」会误报 STALE。


def _preflight_dir(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    """复刻 preflight 的布局:干净文件 + 改名的扫描器副本(+ 可选探针)。"""
    work = tmp_path / "pf"
    work.mkdir()
    (work / "_scanner.py").write_text(
        SCANNER.read_text(encoding="utf-8"), encoding="utf-8")
    (work / "clean.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    for name, body in (extra or {}).items():
        (work / name).write_text(body, encoding="utf-8")
    return work


def _run_image_gate(work: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(work / "_scanner.py"), "--root", str(work),
         "--mode", "image"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def test_renamed_scanner_copy_does_not_scan_itself(tmp_path: Path) -> None:
    """反向对照 ①:干净文件 + `_scanner.py` ⇒ passed。

    扫描器源码里有整套校准样本(前缀、正则、人造载荷),扫到自己必然满仓红。
    自排除必须按**内容标记**判,按文件名在这里就失效了。
    """
    result = _run_image_gate(_preflight_dir(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout
    assert "_scanner.py" not in result.stderr


def test_a_real_violation_beside_the_scanner_copy_still_fails(tmp_path: Path) -> None:
    """反向对照 ②:同目录再放一个 PASSWORD 硬编码 ⇒ failed。

    与上一臂成对 —— 没有它,上一臂的 passed 读不出「闸口还活着」。
    """
    work = _preflight_dir(
        tmp_path, {"probe.py": 'DB_PASSWORD = "Qw8vN2pL5xTz"\n'})
    result = _run_image_gate(work)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "HARDCODED_CREDENTIAL:DB_PASSWORD" in result.stderr
    assert "probe.py" in result.stderr


def test_out_of_scope_register_entries_are_not_reported_stale(
    tmp_path: Path,
) -> None:
    """登记表里不在扫描范围内的条目 ⇒ 「不在范围」,不是「已拆除」。

    🔴 没有读数 ≠ 读数为 0。参见 a-gate-that-cannot-say-it-did-not-run。
    """
    result = _run_image_gate(_preflight_dir(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STALE_TRANSITIONAL_REGISTER" not in result.stderr
    assert "register_out_of_scope=" in result.stdout
    total = len(secret_scanner.TRANSITIONAL_EXPOSURES) + len(
        secret_scanner.FIXTURE_CREDENTIALS)
    assert f"register_out_of_scope={total}" in result.stdout


def test_stale_detection_still_fires_when_the_file_is_in_scope() -> None:
    """反向对照 ③:文件**在场**而命中没了 ⇒ 仍然 STALE 红。

    与上一臂成对:上一臂放松的只是「不在场」,不是「在场但已拆除」。
    [WO_247 改] 用合成登记条目,不再取 TRANSITIONAL_EXPOSURES[0](它已清空)。
    """
    entry = _synth_entry()
    kept = apply_transitional_register([], register=(entry,), scanned={entry.path})
    assert len(kept) == 1
    assert kept[0].code.startswith("STALE_TRANSITIONAL_REGISTER:")

    # 同一条,文件不在场 ⇒ 不报。
    assert apply_transitional_register([], register=(entry,), scanned=set()) == []

def test_pass_line_reports_what_was_actually_suppressed_not_the_declared_total(
    tmp_path: Path,
) -> None:
    """🔴 报**本次真的放行了几条**,不是登记表的账面总数。

    账面非 0 而本次一条都没在场时,必须印 transitional=0 且不带 ⚠。
    [WO_247 改] 账面用合成登记表造出来(真实登记表已清零)。
    """
    work = tmp_path / "w2"
    work.mkdir()
    (work / "clean.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    scanner = _scanner_with_register(work, "    " + repr_synth_entry(count=3) + ",\n")
    # 🔴 承重断言:证明这份扫描器副本的【账面】确实非 0。
    #   没有这一句,本臂在「登记表本来就空」时也会绿 —— 那正是它要守的区别,
    #   而它自己会分不出来(WO_247 下毒时实测:不注入合成登记表,本臂不红)。
    declared = scanner.read_text(encoding="utf-8")
    assert "_synthetic_wo247" in declared, "账面为空,本臂没有分辨力"
    assert "count=3" in declared, "账面数没写进去"

    result = _run_gate_with(work, mode="image")
    assert result.returncode == 0, result.stderr
    # 合成登记指向 docs/AI-CONTEXT/_synthetic_wo247.md,该目录在本 work 里不存在
    # ⇒ 账面 3、实际放行 0,那行必须印实际值。
    assert "transitional=0" in result.stdout, result.stdout
    assert "fixtures=0" in result.stdout, result.stdout
    assert "\u26a0" not in result.stdout, "本次放行 0 ⇒ 那行应是纯绿"

def test_the_self_exclusion_marker_appears_in_exactly_one_repo_file() -> None:
    """自排除标记不得变成「贴一行就能让扫描器闭嘴」的后门。"""
    import subprocess as _sp
    out = _sp.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                  capture_output=True, check=True).stdout.split(b"\0")
    holders = []
    for raw in out:
        if not raw:
            continue
        rel = raw.decode("utf-8")
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if secret_scanner.SELF_EXCLUSION_MARKER in text:
            holders.append(rel)
    assert holders == ["scripts/scan_repository_secrets.py"], holders


# ---------------------------------------------------------------------------
# [WO_247 · 2026-09-22] 下面这批臂原本直接吃 `secret_scanner.TRANSITIONAL_EXPOSURES`。
# WO_247 把那 10 处真凭据清掉后登记表归零,它们**全部转红** —— 但红的不是被测性质,
# 是判据自己的前提没了(「仍然存在」型锁必须与修法同班退役)。
# 🔴 原文件里 `test_pass_line_states_the_register_counts_when_nonzero` 就写着
#    「本臂的前提是登记表非空;空了要改判据不是删断言」—— 照办:改成合成登记表。
# 从此这些臂不再依赖仓里真有暴露,性质却照守。
# ---------------------------------------------------------------------------

_SYNTH_DOC = "docs/AI-CONTEXT/_synthetic_wo247.md"


def _synth_entry(count: int = 1) -> TransitionalExposure:
    """一条**本文件现造**的登记,指向合成路径,与仓里任何真实暴露无关。"""
    return TransitionalExposure(
        path=_SYNTH_DOC,
        code="TOKEN_SHAPE:GITHUB_PAT",
        count=count,
        reason="WO_247 判据用合成样本",
        tracking="WO_247(合成夹具,不对应任何真实暴露)",
    )


def _scanner_with_register(work: Path, entries: str) -> Path:
    """把扫描器抄一份到 work/,并把 TRANSITIONAL_EXPOSURES 换成合成条目。

    🔴 必须断言替换真的发生 —— 锚没命中却照写,会让下面每条臂都测了个寂寞。
    """
    src = SCANNER.read_text(encoding="utf-8")
    anchor = "TRANSITIONAL_EXPOSURES: tuple[TransitionalExposure, ...] = ("
    assert src.count(anchor) == 1, "扫描器里登记表声明不唯一,锚失效"
    head, _, rest = src.partition(anchor)
    close = rest.index("\n)\n")
    patched = head + anchor + "\n" + entries + rest[close:]
    assert "_synthetic_wo247" in patched, "合成条目没写进去"
    out = work / "_scanner.py"
    out.write_text(patched, encoding="utf-8")
    return out


def _run_gate_with(work: Path, mode: str = "image", report: bool = False):
    cmd = [sys.executable, str(work / "_scanner.py"), "--root", str(work), "--mode", mode]
    if report:
        cmd.append("--report")
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _work_with_synth_hit(tmp_path: Path) -> Path:
    """合成目录:一个 docs 路径下含合成 PAT 的文件 + 一个干净文件。"""
    work = tmp_path / "w"
    (work / "docs" / "AI-CONTEXT").mkdir(parents=True)
    (work / "docs" / "AI-CONTEXT" / "_synthetic_wo247.md").write_text(
        "token = " + _FAKE_CLASSIC_PAT + "\n", encoding="utf-8")
    (work / "clean.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    return work


def repr_synth_entry(count: int = 1) -> str:
    """把合成登记条目渲染成可写进扫描器副本的源码字面量。"""
    return (
        "TransitionalExposure(path=%r, code=%r, count=%d, reason=%r, tracking=%r)"
        % (_SYNTH_DOC, "TOKEN_SHAPE:GITHUB_PAT", count,
           "WO_247 判据用合成样本", "WO_247(合成夹具)")
    )
