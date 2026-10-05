from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from scripts import scan_repository_secrets as secret_scanner
from scripts.scan_repository_secrets import KnownSecret, scan_history, scan_text


ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "scripts" / "scan_repository_secrets.py"


def _codes(text: str, *, known_secrets=()) -> set[str]:
    return {
        finding.code
        for finding in scan_text(
            relative="services/example.py",
            text=text,
            known_secrets=known_secrets,
        )
    }


def test_known_secret_fingerprint_detects_value_without_storing_it() -> None:
    value = "fixture-secret-that-is-not-production"
    known = (
        KnownSecret(
            label="fixture",
            sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            length=len(value),
        ),
    )
    assert _codes(f'PASSWORD = "{value}"', known_secrets=known) == {
        "KNOWN_SECRET:fixture",
        "HARDCODED_CREDENTIAL:PASSWORD",
    }


def test_high_confidence_secret_patterns_are_blocked() -> None:
    codes = _codes(
        "\n".join(
            (
                'API_KEY = "live-looking-value-123456"',
                # [WO_245 三轮] 主机由 example.invalid 改为真域名:
                # example.invalid 是 RFC 2606 保留域,新增的保留域规则会豁免它
                # —— 正样本臂若挑一个「按规则就该放行」的主机,这一臂就空转了。
                'URL = "https://root:password@omnirank.top/path"',
                "ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())",
                "-----BEGIN OPENSSH PRIVATE KEY-----",
            )
        )
    )
    assert "HARDCODED_CREDENTIAL:API_KEY" in codes
    assert "CREDENTIAL_URL" in codes
    assert "SSH_AUTO_ADD_POLICY" in codes
    assert "PRIVATE_KEY" in codes


def test_environment_and_pinned_host_key_patterns_are_allowed() -> None:
    assert not _codes(
        "\n".join(
            (
                'API_KEY = os.environ["API_KEY"]',
                "ssh.load_system_host_keys()",
                "ssh.set_missing_host_key_policy(paramiko.RejectPolicy())",
            )
        )
    )


def test_tv_password_hash_has_no_source_fallback() -> None:
    source = (ROOT / "api" / "dashboard_api.py").read_text(encoding="utf-8")
    assert "_TV_LEGACY_PASSWORD_HASH" not in source
    assert 'os.getenv("TV_ACCESS_PASSWORD_LEGACY_HASH")' in source


def test_monitoring_clear_password_is_secret_store_only() -> None:
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "CLEAR_DATA_PASSWORD =" not in source
    assert 'os.getenv("MONITORING_CLEAR_DATA_PASSWORD"' in source
    assert "hmac.compare_digest" in source
    assert "MONITORING_CLEAR_DATA_PASSWORD_NOT_CONFIGURED" in source


def test_retired_monitoring_clear_password_fingerprint_is_registered() -> None:
    registered = {
        (secret.label, secret.sha256, secret.length)
        for secret in secret_scanner.KNOWN_SECRETS
    }
    assert (
        "retired_monitoring_clear_password",
        "31856251d5cf4af67e562ba30e302bc5cafa694e344f5cd8b1850ade5eedeebb",
        12,
    ) in registered


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Secret Gate Test",
            "-c",
            "user.email=secret-gate@example.invalid",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )


def test_history_mode_detects_deleted_secret_and_accepts_clean_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    value = "fixture-history-secret-not-production"
    known = (
        KnownSecret(
            label="deleted_fixture",
            sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            length=len(value),
        ),
    )
    monkeypatch.setattr(secret_scanner, "KNOWN_SECRETS", known)

    contaminated = tmp_path / "contaminated"
    contaminated.mkdir()
    subprocess.run(["git", "-C", str(contaminated), "init", "-q"], check=True)
    secret_file = contaminated / "retired_helper.py"
    secret_file.write_text(f'PASSWORD = "{value}"\n', encoding="utf-8")
    _commit(contaminated, "add retired helper")
    secret_file.unlink()
    _commit(contaminated, "remove retired helper")

    findings = scan_history(contaminated)
    assert {finding.code for finding in findings} == {
        "KNOWN_SECRET:deleted_fixture"
    }
    assert all("retired_helper.py" in finding.path for finding in findings)

    clean = tmp_path / "clean"
    clean.mkdir()
    subprocess.run(["git", "-C", str(clean), "init", "-q"], check=True)
    (clean / "safe.py").write_text(
        'PASSWORD = os.environ["PASSWORD"]\n',
        encoding="utf-8",
    )
    _commit(clean, "add environment-backed credential")
    assert scan_history(clean) == []


def test_current_tracked_tree_passes_secret_gate() -> None:
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(ROOT), "--mode", "tracked"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_docker_context_excludes_private_helpers_and_runs_image_gate() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "scripts/_*.py" in dockerignore
    assert ".planning/" in dockerignore
    copy_offset = dockerfile.index("COPY . .")
    dist_offset = dockerfile.index(
        "COPY --from=frontend-builder /app/frontend/dist ./frontend/dist"
    )
    scan_offset = dockerfile.index(
        "python scripts/scan_repository_secrets.py --root /app --mode image"
    )
    assert scan_offset > copy_offset
    assert scan_offset > dist_offset


def test_scanner_exposes_full_history_mode_for_post_rotation_rewrite_gate() -> None:
    source = SCANNER.read_text(encoding="utf-8")
    assert 'choices=("tracked", "image", "history")' in source
    assert "git\", \"-C\", str(root), \"rev-list\", \"--objects\", \"--all\"" in source
