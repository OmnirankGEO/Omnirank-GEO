"""WO_261 ④ · 公开快照门 `--public` 的判据。

**要守的那句话**:私仓的过渡豁免是一条**内部约定**(「这 10 处 PAT 已登记,
等 WO_247 清」);快照一旦公开,那 10 处就是 10 处明文凭据 —— 约定不跟着走。
⇒ `--public` 下 `TRANSITIONAL_EXPOSURES` **一条都不放行**,硬条件是
  pass 行 `transitional=0` 且 `pending_tracking=0`(OSS_09 v3 §8.3)。

🔴 同时必须钉住**私仓默认模式一个字没变** —— Deploy 现役 preflight 第 5-b 关在用它。
   一把尺子加新模式时最容易出的事,是顺手把旧模式的行为也改了。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import scan_repository_secrets as secret_scanner

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "scan_repository_secrets.py"

_ALPHABET = "aB3dE7fG1hJ4kL9mN2pQ6rS8tU0vW5xY3zA7bC4iK6oR2uV8wX1yZ5"


def _body(n: int) -> str:
    out = (_ALPHABET * (n // len(_ALPHABET) + 1))[:n]
    assert len(out) == n and len(set(out)) > 6
    return out


FAKE_PAT = "ghp_" + _body(36)


def _init_repo(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(p), "init", "-q"], check=True)


def _commit(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A", "--force"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=WO261", "-c",
         "user.email=wo261@example.invalid", "commit", "-m", "f"],
        check=True, capture_output=True,
    )


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(repo), "--mode", "tracked", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _clean_tree(tmp_path: Path) -> Path:
    repo = tmp_path / "clean"
    _init_repo(repo)
    (repo / "app.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    _commit(repo)
    return repo


# ------------------------------------------------------------------ 绿的那半


def test_public_gate_is_green_on_a_clean_tree(tmp_path: Path) -> None:
    """干净树:rc=0,且 pass 行必须**明写** transitional=0 pending_tracking=0。"""
    result = _run(_clean_tree(tmp_path), "--public")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "secret_scan=passed" in result.stdout
    assert "transitional=0" in result.stdout
    assert "pending_tracking=0" in result.stdout
    # 干净树上没有任何东西被放行 ⇒ 不该带 ⚠(带了就读不出「真干净」)
    assert "⚠" not in result.stdout


# ------------------------------------------------------------------ 红的那半


def test_public_gate_rejects_an_injected_pat(tmp_path: Path) -> None:
    """反臂:塞一枚形状正确的假 PAT ⇒ 必红。"""
    repo = _clean_tree(tmp_path)
    (repo / "notes.md").write_text(f"token: {FAKE_PAT}\n", encoding="utf-8")
    _commit(repo)

    result = _run(repo, "--public")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "TOKEN_SHAPE:GITHUB_PAT" in result.stderr
    assert FAKE_PAT not in result.stdout, "值不得出现在闸口输出里"


def test_public_gate_does_not_honour_a_transitional_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 本文件的主判据:**同一棵树,私仓模式绿、公开模式红**。

    树里塞一枚 PAT,并给它配一条过渡登记。
    私仓模式:登记放行 ⇒ 绿(这是现役行为,不许变)。
    公开模式:登记不算数 ⇒ 红。
    两个读数必须**相反** —— 若公开模式也绿,说明豁免跟着包走了,
    那正是 Codex B1 点名的那件事。
    """
    repo = _clean_tree(tmp_path)
    (repo / "notes.md").write_text(f"token: {FAKE_PAT}\n", encoding="utf-8")
    _commit(repo)

    entry = secret_scanner.TransitionalExposure(
        path="notes.md", code="TOKEN_SHAPE:GITHUB_PAT", count=1,
        reason="判据用的人造登记", tracking="WO_261 测试夹具",
    )
    monkeypatch.setattr(secret_scanner, "TRANSITIONAL_EXPOSURES", (entry,))

    # 私仓口径:登记在册 ⇒ 放行
    kept_private = secret_scanner.apply_transitional_register(
        [secret_scanner.Finding("notes.md", 1, "TOKEN_SHAPE:GITHUB_PAT")],
        scanned={"notes.md"},
    )
    assert kept_private == [], "私仓模式应当放行已登记的命中(现役行为)"

    # 公开口径:只认 FIXTURE_CREDENTIALS ⇒ 不放行
    kept_public = secret_scanner.apply_transitional_register(
        [secret_scanner.Finding("notes.md", 1, "TOKEN_SHAPE:GITHUB_PAT")],
        register=secret_scanner.FIXTURE_CREDENTIALS,
        scanned={"notes.md"},
    )
    assert len(kept_public) == 1 and kept_public[0].code == "TOKEN_SHAPE:GITHUB_PAT", (
        "公开门放行了一条过渡登记 —— 私仓的内部约定跟着包走了"
    )


# -------------------------------------------------- 私仓默认模式不许被顺手改


def test_private_default_mode_still_honours_the_register(monkeypatch) -> None:
    """Deploy 现役 preflight 5-b 用的就是默认模式:它必须照旧放行登记项。

    这一格与上一格成对:没有它,「公开门红」可能是因为**整个登记机制被我改坏了**,
    而不是因为公开门刻意不认登记。

    🔴 [WO_247 · 2026-09-22 Deploy 改] 原版读**真实** TRANSITIONAL_EXPOSURES 并硬断言
       它非空(`assert live, "本仓当前应当有过渡登记(WO_247 的 10 处 PAT)"`)。
       WO_247 把那 10 处清干净后,这一格当场**真红** —— 它属于「缺陷仍然存在」型
       交接锁,必须与修法同班退役,而不是留着让下一班撞。
       改法沿用**上一格已有的写法**:喂合成登记条目。这样它验的是
       「登记机制通不通」,与真表里此刻有没有条目**脱钩** ——
       真表再被清空或再被填满,这一格都照样有牙。

       🔴 它当初为什么没被任何门逮到:本包不在 `tests/MUST_RUN.txt` 里(G2b 不跑它),
       又早已在 main 上(5-d 棘轮只管「新增未登记」,存量被祖父化)。
       ⇒ 存量无运行者的测试包被别的单打红时,两道门都不会喊。
       [WO_266 · 2026-09-23] 上面是当时的事实,**现已不成立**:G2b 的 ONESHOT 段
       每班全跑 `tests/ONESHOT_RUNS.txt` 全集(本包在列),红了当班就拦。
    """
    entry = secret_scanner.TransitionalExposure(
        path="notes.md", code="TOKEN_SHAPE:GITHUB_PAT", count=2,
        reason="判据用的人造登记", tracking="WO_247 测试夹具",
    )
    monkeypatch.setattr(secret_scanner, "TRANSITIONAL_EXPOSURES", (entry,))
    # 🔴 要喂**恰好 count 条**。第一版只喂 1 条,而这条登记 count=2 ⇒
    #    登记表按「实测少于登记」判成陈账当场红 —— 是我的夹具错,不是代码错。
    #    (精确计数逮到了自己的判据,这本身就是那格锁在起作用。)
    kept = secret_scanner.apply_transitional_register(
        [secret_scanner.Finding(entry.path, i + 1, entry.code)
         for i in range(entry.count)],
        scanned={entry.path},
    )
    assert kept == [], "默认模式不再放行登记项 ⇒ 私仓行为被改坏了"


def test_public_flag_is_wired_and_documented() -> None:
    source = SCANNER.read_text(encoding="utf-8")
    assert '"--public"' in source
    assert "public_register" in source
    # 公开门的两个硬条件必须在代码里**自证**,不是只写在文档里
    assert "transitional 必须为 0" in source
