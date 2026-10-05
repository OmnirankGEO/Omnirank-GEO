"""preflight 5-e · 新增路由归属门(scripts/gate_new_routes_mapped.py)的牙证与对照臂。

被测物按 `--repo/--ref/--base` 跑在临时合成仓上(与 preflight 同一条读取路径:只读 git 对象)。
合成仓里放一份最小的 module_mapping / middleware / 路由文件,每格只改一处,让一个条件决定结果;
每条会红的格旁边都有一条同形但该绿的对照。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "gate_new_routes_mapped.py"

MAPPING = '''from typing import Optional
ROUTE_PREFIX_MAP = [
    ("/api/known/", "known_mod"),
    ("/api/authonly/", None),
]


def resolve_permission(path: str) -> Optional[str]:
    for prefix, module in ROUTE_PREFIX_MAP:
        if path.startswith(prefix):
            return module
    return "__unmapped__"
'''
MIDDLEWARE = '''PUBLIC_PATHS = {"/api/health"}
PUBLIC_PREFIXES = ("/api/public/", "/api/portal/")
PORTAL_PROTECTED_PREFIXES = ("/api/portal/tokens",)
PUBLIC_SUFFIXES = ("/avatar-image",)
'''
BASE_API = '''from fastapi import APIRouter
router = APIRouter(prefix="/api/known")


@router.get("/list")
def a():
    pass
'''
ALLOW = "tests/route_permission_mapping_2026_09_24/admin_only_allowlist.tsv"


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env.update(GIT_AUTHOR_NAME="b", GIT_AUTHOR_EMAIL="b@example.invalid", GIT_COMMITTER_NAME="b",
               GIT_COMMITTER_EMAIL="b@example.invalid", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=_env())
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("routegate")
    _git(path, "init", "-q")
    _git(path, "config", "core.autocrlf", "false")
    for rel, text in {"auth/module_mapping.py": MAPPING, "auth/middleware.py": MIDDLEWARE,
                      "api/base_api.py": BASE_API}.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(text.encode("utf-8"))
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _commit(repo: Path, base: str, changes: dict[str, str]) -> str:
    _git(repo, "checkout", "-q", "--detach", base)
    for rel, text in changes.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "pkg")
    return _git(repo, "rev-parse", "HEAD")


def _argv(repo: Path, ref: str, base: str, min_routes: int | None) -> list[str]:
    """min_routes=None ⇒ 不传 --min-routes,与 preflight 5-e 的真实调用形态一致(靠默认值)。"""
    extra = [] if min_routes is None else ["--min-routes", str(min_routes)]
    return ["--repo", str(repo), "--ref", ref, "--base", base, *extra]


def _run_cli(repo: Path, ref: str, base: str, min_routes: int | None = 1) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(GATE), *_argv(repo, ref, base, min_routes)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=_env())
    return r.returncode, r.stdout + r.stderr


_GATE_MOD = None


def _run(repo: Path, ref: str, base: str, min_routes: int | None = 1) -> tuple[int, str]:
    """[AE 提速] 进程内调仓里这一份门的 main();与 CLI 等价由 test_in_process_and_cli_give_identical_readings 逐字核。"""
    global _GATE_MOD
    if _GATE_MOD is None:
        spec = importlib.util.spec_from_file_location("_routegate_under_test", GATE)
        _GATE_MOD = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_GATE_MOD)
    saved = {k: os.environ.pop(k) for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE") if k in os.environ}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = _GATE_MOD.main(_argv(repo, ref, base, min_routes))
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
    finally:
        os.environ.update(saved)
    return rc, out.getvalue() + err.getvalue()


def _api(prefix: str, *decorators: str) -> str:
    body = "".join(f"\n\n{d}\ndef f{i}():\n    pass\n" for i, d in enumerate(decorators))
    return f'from fastapi import APIRouter\nrouter = APIRouter(prefix="{prefix}")\n{body}'


@pytest.mark.parametrize("scenario", ["green", "red", "rc3-default-floor"])
def test_in_process_and_cli_give_identical_readings(repo, scenario):
    """[AE 提速] 其余格走进程内;这一格核「进程内 == CLI」:退出码与整段输出逐字相同。"""
    r, base = repo
    changes = {"green": {"README.md": "cli parity\n"},
               "red": {"api/new_api.py": _api("/api/brandnew", '@router.get("/dict")')},
               "rc3-default-floor": {"README.md": "floor\n"}}[scenario]
    mr = None if scenario == "rc3-default-floor" else 1
    h = _commit(r, base, changes)
    got_in, got_cli = _run(r, h, base, mr), _run_cli(r, h, base, mr)
    assert (got_in[0], got_in[1].strip()) == (got_cli[0], got_cli[1].strip()), f"进程内:\n{got_in[1]}\nCLI:\n{got_cli[1]}"
    assert got_in[0] == {"green": 0, "red": 1, "rc3-default-floor": 3}[scenario], got_in[1]


def test_no_new_route_is_no_object_not_green_claim(repo):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"README.md": "x\n"}), base)
    assert rc == 0, out
    assert "本包没有新增 /api 路由(无对象)" in out, out


def test_a_new_unmapped_route_is_red(repo):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"api/new_api.py": _api("/api/brandnew", '@router.get("/dict")')}), base)
    assert rc == 1, out
    assert "/api/brandnew/dict [GET] 没有模块归属" in out, out


def test_the_same_route_once_mapped_is_green(repo):
    r, base = repo
    mapping = MAPPING.replace('("/api/authonly/", None),', '("/api/authonly/", None),\n    ("/api/brandnew", None),')
    rc, out = _run(r, _commit(r, base, {"api/new_api.py": _api("/api/brandnew", '@router.get("/dict")'),
                                        "auth/module_mapping.py": mapping}), base)
    assert rc == 0, out
    assert "✅ /api/brandnew/dict → None(仅需认证)" in out, out


def test_a_new_route_under_a_mapped_prefix_with_a_path_param_is_green(repo):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"api/base_api.py": BASE_API + '\n\n@router.delete("/{item_id}")\ndef b():\n    pass\n'}), base)
    assert rc == 0, out
    assert "✅ /api/known/{item_id} → known_mod" in out, out


@pytest.mark.parametrize("prefix, deco, want_rc, needle", [
    ("/api/public", '@router.get("/share")', 0, "⚪ /api/public/share  公开路由"),
    ("/api/portal", '@router.get("/view")', 0, "⚪ /api/portal/view  公开路由"),
    ("/api/portal", '@router.post("/tokens/issue")', 1, "/api/portal/tokens/issue [POST] 没有模块归属"),
    ("/api/x", '@router.get("/avatar-image")', 0, "⚪ /api/x/avatar-image  公开路由"),
    ("/api/x", '@router.post("/avatar-image")', 1, "/api/x/avatar-image [POST] 没有模块归属"),
], ids=["public-prefix", "portal-public", "portal-protected-is-not-public",
        "suffix-get-public", "suffix-post-not-public"])
def test_public_judgement_mirrors_the_middleware(repo, prefix, deco, want_rc, needle):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"api/pub_api.py": _api(prefix, deco)}), base)
    assert rc == want_rc, out
    assert needle in out, out


def test_app_api_route_in_server_is_seen(repo):
    r, base = repo
    server = ('from fastapi import FastAPI\napp = FastAPI()\n\n\n'
              '@app.api_route("/api/srv/thing", methods=["GET", "POST"])\ndef t():\n    pass\n')
    rc, out = _run(r, _commit(r, base, {"server.py": server}), base)
    assert rc == 1, out
    assert "/api/srv/thing [GET,POST] 没有模块归属" in out, out


def test_an_allowlisted_admin_only_route_is_named_not_red(repo):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {
        "api/new_api.py": _api("/api/brandnew", '@router.get("/dict")'),
        ALLOW: "# 路径<TAB>类别<TAB>原因\n/api/brandnew/dict\tNO_FRONTEND\t合成:刻意只给 admin\n"}), base)
    assert rc == 0, out
    assert "⚠️  /api/brandnew/dict  未映射 = 只 admin 可用,已写进 admin_only_allowlist.tsv" in out, out


def test_a_new_route_with_an_unresolvable_prefix_is_rc3(repo):
    r, base = repo
    src = 'from fastapi import APIRouter\nP = "/api/dyn"\nrouter = APIRouter(prefix=P)\n\n\n@router.get("/a")\ndef a():\n    pass\n'
    rc, out = _run(r, _commit(r, base, {"api/dyn_api.py": src}), base)
    assert rc == 3, out
    assert "算不出完整路径" in out, out


@pytest.mark.parametrize("mapping, needle", [
    ("ROUTE_PREFIX_MAP = []\n\n\ndef resolve_permission(p):\n    return '__unmapped__'\n", "映射表第一条前缀"),
    # 表还在,只把默认值从 __unmapped__ 改成放行(fail-open)⇒ 探针自证必须拦下,否则所有新路由都「有归属」
    (MAPPING.replace('return "__unmapped__"', "return None"), "探针路径没回 __unmapped__"),
], ids=["no-prefix-map", "default-fail-open"])
def test_a_mapping_whose_semantics_changed_is_rc3(repo, mapping, needle):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"auth/module_mapping.py": mapping,
                                        "api/new_api.py": _api("/api/brandnew", '@router.get("/dict")')}), base)
    assert rc == 3, out
    assert needle in out, out


@pytest.mark.parametrize("server, want_rc", [
    ('from fastapi import FastAPI\nfrom api.base_api import router\napp = FastAPI()\napp.include_router(router, prefix="/v2")\n', 3),
    ('from fastapi import APIRouter\nfrom api.base_api import router as sub\nouter = APIRouter()\nouter.include_router(sub)\n', 3),
    ('from fastapi import FastAPI\nfrom api.base_api import router\napp = FastAPI()\napp.include_router(router)\n', 0),
], ids=["include-with-prefix", "router-in-router", "plain-include-control"])
def test_breaking_the_static_path_premise_is_rc3(repo, server, want_rc):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"server.py": server}), base)
    assert rc == want_rc, out
    if want_rc == 3:
        assert "静态完整路径的前提被打破" in out, out


def test_the_default_denominator_floor_holds_when_called_like_preflight(repo):
    """[WO_294 Review 补] preflight 5-e 不传 --min-routes,分母哨兵全靠默认值。
    小夹具只抽得到几条路由 ⇒ 默认值下必须 rc 3;默认值被改小(如 0)⇒ 本格红。"""
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"api/new_api.py": _api("/api/brandnew", '@router.get("/dict")')}),
                   base, min_routes=None)
    assert rc == 3, out
    assert "分母塌了" in out, out


def test_a_collapsed_denominator_is_rc3(repo):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"README.md": "y\n"}), base, min_routes=800)
    assert rc == 3, out
    assert "分母塌了" in out, out


# ─── AG 补 · 工厂路由器 + 子应用挂载点 ───────────────────
# 真实路径 = MOUNT_PATH + 工厂调用的 prefix + 装饰器路径;工厂没有 MOUNT_PATH ⇒ rc 3(不猜挂在哪)。

def _sub_app(mount: str | None, *, factory_prefix: str = "", body: str = '@open_router.get("/items")\ndef f():\n    pass\n') -> str:
    head = "from fastapi import APIRouter, FastAPI\n"
    if mount is not None:
        head += f"MOUNT_PATH = {mount}\n"
    fp = f'prefix="{factory_prefix}", ' if factory_prefix else ""
    return (head + f"\n\ndef new_open_router() -> APIRouter:\n    return APIRouter({fp}tags=[\"x\"])\n\n\n"
            f"open_router = new_open_router()\n\n\n{body}")


def test_factory_routers_resolve_under_mount_path(repo):
    """一次提交里放三个子应用文件 + 一个普通文件,每条路由的归类各自点名:
    /api/foo/v1(未映射)⇒ 红(工厂 · 工厂调用带 prefix · 同文件普通 APIRouter 三种都加 MOUNT_PATH);
    /api/public/open/v1 ⇒ 公开不在范围;/api/known/v1 ⇒ 有归属;别的文件的普通路由不被加前缀(对照)。"""
    r, base = repo
    foo_body = ('sub_router = new_open_router(prefix="/sub")\nplain = APIRouter()\n\n\n'
                '@open_router.get("/items")\ndef f1():\n    pass\n\n\n'
                '@sub_router.get("/items")\ndef f2():\n    pass\n\n\n'
                '@plain.post("/p")\ndef f3():\n    pass\n')
    rc, out = _run(r, _commit(r, base, {
        "api/sub_foo_api.py": _sub_app('"/api/foo/v1"', body=foo_body),
        "api/sub_pub_api.py": _sub_app('"/api/public/open/v1"'),
        "api/sub_known_api.py": _sub_app('"/api/known/v1"'),
        "api/new_api.py": _api("/api/brandnew", '@router.get("/dict")')}), base)
    assert rc == 1, out
    for needle in ("/api/foo/v1/items [GET] 没有模块归属", "/api/foo/v1/sub/items [GET] 没有模块归属",
                   "/api/foo/v1/p [POST] 没有模块归属", "⚪ /api/public/open/v1/items  公开路由",
                   "✅ /api/known/v1/items → known_mod", "/api/brandnew/dict [GET] 没有模块归属"):
        assert needle in out, (needle, out)
    assert "本包新增 6 条" in out or "本包新增 6(" in out, out
    assert "/api/public/open/v1/api/" not in out and "/api/known/v1/api/" not in out, out


@pytest.mark.parametrize("src, needle", [
    (_sub_app(None), "工厂路由器所在文件没声明 MOUNT_PATH"),
    (_sub_app('"/api/" + "foo"'), "前缀或路径不是常量"),
    (_sub_app('"/api/foo/v1"', factory_prefix="/hidden"), "前缀或路径不是常量"),
    (_sub_app('"/api/foo/v1"', body='app = FastAPI()\n\n\n@app.get("/api/host/thing")\ndef f():\n    pass\n'),
     "分不清 app 是宿主还是子应用"),
], ids=["factory-no-mount", "mount-not-literal", "factory-body-prefix", "app-in-mount-file"])
def test_factory_router_that_cannot_be_placed_is_rc3(repo, src, needle):
    r, base = repo
    rc, out = _run(r, _commit(r, base, {"api/open_sub_api.py": src}), base)
    assert rc == 3, out
    assert needle in out, out
