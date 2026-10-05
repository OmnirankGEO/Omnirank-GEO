"""WO_330 · 镜像 Python 依赖全量版本锁(2026-10-02):requirements-docker-constraints.txt 与 Dockerfile 的 -c。

0913AQ 重建依赖层时,没锁版本的传递依赖从镜像源拿了新版本,漂了 16 个(SQLAlchemy 2.0 → 2.1、Markdown 3.10 → 3.11 等)。
WO_330 用 AQ 生产镜像的 pip 清单(去掉 pip 自身,238 条)做 constraints,Dockerfile 两处 pip install 都带 -c。

守三件事(纯静态,不起应用,不连网,不连库):
1. 锁文件本身:每条都是 `名==版本`(没有直链 / extras / 环境标记),不重名,不含 pip 自身;
2. 与 requirements-docker.txt 一致:requirements 里的每个包锁文件里都有,且锁住的版本满足 requirements 的写法
   (`==` 必须同版本,`>=` 必须不低于下限)—— 不一致的话 bake 在 pip 那一步报 ResolutionImpossible,本格让它在 preflight 就红;
3. Dockerfile:pip install 恰两处(audioop-lts 假包 + requirements),都带 `-c requirements-docker-constraints.txt`;
   锁文件由一条 COPY 在 pip 那条 RUN 之前拷进镜像。
每个判据都是一个函数:吃仓里的原文必须报 0 条问题(对照臂),吃改坏的文本必须报出问题(牙证),两条臂都在格内。
「用它 bake 出来的 pip 清单与 AQ 差 0 行」是 bake 读数,不在本包。
"""
from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parents[2]
CONSTRAINTS = "requirements-docker-constraints.txt"
REQUIREMENTS = "requirements-docker.txt"

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!_-]+)$")
_PIP_INSTALL = re.compile(r"\bpip3?\s+install\b([^&;|]*)")
_WITH_CONSTRAINT = re.compile(r"(?:^|\s)(?:-c|--constraint)(?:\s+|=)requirements-docker-constraints\.txt(?=\s|$)")


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _payload(text: str) -> list[str]:
    """去掉注释与空行(requirements / constraints 的注释以 # 起)。"""
    out = []
    for raw in text.splitlines():
        s = raw.split("#", 1)[0].strip()
        if s:
            out.append(s)
    return out


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def constraint_problems(text: str) -> list[str]:
    problems, seen = [], {}
    for s in _payload(text):
        m = _PIN.match(s)
        if not m:
            problems.append(f"不是 `名==版本`:{s}")
            continue
        name = _canon(m.group(1))
        if name == "pip":
            problems.append(f"含 pip 自身:{s}")
        if name in seen:
            problems.append(f"重名:{seen[name]} / {s}")
        seen[name] = s
    return problems


def _pins(text: str) -> dict[str, Version]:
    return {_canon(m.group(1)): Version(m.group(2)) for m in map(_PIN.match, _payload(text)) if m}


def consistency_problems(requirements_text: str, constraints_text: str) -> list[str]:
    pins, problems = _pins(constraints_text), []
    for s in _payload(requirements_text):
        req = Requirement(s)
        name = _canon(req.name)
        if name not in pins:
            problems.append(f"requirements 有、锁文件没有:{s}")
        elif not req.specifier.contains(pins[name], prereleases=True):
            problems.append(f"requirements 写 {s},锁文件锁的是 {pins[name]}")
    return problems


def _instructions(dockerfile: str) -> list[str]:
    """按 Dockerfile 语义拆成逻辑指令:整行注释丢掉(续行中间的也丢),行尾反斜杠续行拼成一条。"""
    lines = [ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#")]
    return [ins.strip() for ins in re.sub(r"\\[ \t]*\n", " ", "\n".join(lines)).split("\n") if ins.strip()]


def dockerfile_problems(dockerfile: str) -> list[str]:
    ins = _instructions(dockerfile)
    problems = []
    calls = [(i, m.group(1)) for i, s in enumerate(ins) if s.upper().startswith("RUN ") for m in _PIP_INSTALL.finditer(s)]
    if len(calls) != 2:
        problems.append(f"pip install 应恰 2 处(audioop-lts 假包 + requirements),实为 {len(calls)} 处")
    for _i, args in calls:
        if not _WITH_CONSTRAINT.search(args):
            problems.append(f"这处 pip install 没带 -c {CONSTRAINTS}:pip install{args.rstrip()}")
    req_run = [i for i, args in calls if re.search(r"-r\s+requirements-docker\.txt\b", args)]
    copies = [i for i, s in enumerate(ins) if s.upper().startswith("COPY ") and CONSTRAINTS in s.split()]
    if len(req_run) != 1:
        problems.append(f"装 requirements-docker.txt 的 pip install 应恰 1 处,实为 {len(req_run)} 处")
    elif not any(c < req_run[0] for c in copies):
        problems.append(f"{CONSTRAINTS} 没有在装依赖那条 RUN 之前被 COPY 进镜像")
    return problems


# ---------------------------------------------------------------- 1. 锁文件本身

def test_constraints_file_well_formed():
    text = _read(CONSTRAINTS)
    assert len(_payload(text)) >= 200, "分母:锁文件条数异常少,是不是读错文件了"
    assert constraint_problems(text) == []


def test_constraints_checker_teeth():
    good = _read(CONSTRAINTS)
    assert constraint_problems(good) == []                                          # 对照臂
    for bad in ("pip==25.0.1\n", "audioop-lts @ file:///tmp/audioop-lts\n", "redis[hiredis]==5.2.1\n",
                "SQLAlchemy==2.0.54\n", "markdown>=3.5\n", "jieba==0.42.1 ; python_version >= '3.12'\n"):
        assert constraint_problems(good.rstrip("\n") + "\n" + bad), bad             # 牙证:每种坏写法单独注入都抓到


# ---------------------------------------------------------------- 2. 与 requirements-docker.txt 一致

def test_requirements_consistent_with_constraints():
    reqs = _read(REQUIREMENTS)
    assert len(_payload(reqs)) >= 100, "分母:requirements 条数异常少"
    assert consistency_problems(reqs, _read(CONSTRAINTS)) == []


def test_consistency_checker_teeth():
    reqs, cons = _read(REQUIREMENTS), _read(CONSTRAINTS)
    assert consistency_problems(reqs, cons) == []                                   # 对照臂
    pinned = next(s for s in _payload(reqs) if "==" in s and "[" not in s)          # 取一条 == 写法,改个版本
    name, ver = pinned.split("==")
    bumped = re.sub(rf"(?m)^{re.escape(pinned)}$", f"{name}=={ver}.post999", reqs, count=1)
    assert bumped != reqs and consistency_problems(bumped, cons)                    # 牙证 1:requirements 改了版本、锁文件没跟
    added = reqs.rstrip("\n") + "\nbrand-new-package==1.0\n"
    assert consistency_problems(added, cons)                                        # 牙证 2:新增包没进锁文件
    low = re.sub(r"(?mi)^markdown==.*$", "Markdown==3.4", cons)
    assert low != cons and consistency_problems(reqs, low)                          # 牙证 3:锁住的版本低于 >= 下限


# ---------------------------------------------------------------- 3. Dockerfile

def test_dockerfile_both_pip_installs_constrained():
    assert dockerfile_problems(_read("Dockerfile")) == []


def test_dockerfile_checker_teeth():
    good = _read("Dockerfile")
    assert dockerfile_problems(good) == []                                          # 对照臂
    assert good.count(f" -c {CONSTRAINTS}") == 2 and good.count(f"-r requirements-docker.txt -c {CONSTRAINTS}") == 1
    cases = {
        "假包那处去掉 -c": good.replace(f"pip install -c {CONSTRAINTS} /tmp/audioop-lts", "pip install /tmp/audioop-lts"),
        "requirements 那处去掉 -c": good.replace(f"-r requirements-docker.txt -c {CONSTRAINTS}", "-r requirements-docker.txt"),
        "COPY 不再拷锁文件": good.replace(f"COPY requirements-docker.txt {CONSTRAINTS} ./", "COPY requirements-docker.txt ./"),
        "多出第三处 pip install": good.replace("    && rm -rf /tmp/audioop-lts \\\n",
                                          "    && rm -rf /tmp/audioop-lts \\\n    && pip install some-extra \\\n"),
        "锁文件名写错": good.replace(f"-r requirements-docker.txt -c {CONSTRAINTS}",
                                 "-r requirements-docker.txt -c requirements-docker-constraints.txt.bak"),
    }
    for why, bad in cases.items():
        assert bad != good, f"牙证没注进去:{why}"
        assert dockerfile_problems(bad), why                                        # 牙证:每种改坏都抓到
    # 对照:注释里提到 pip install(不带 -c)不算 —— 包括续行中间的注释行
    commented = good.replace("    && rm -rf /tmp/audioop-lts \\\n",
                             "    && rm -rf /tmp/audioop-lts \\\n# 旧写法 pip install -r requirements-docker.txt \\\n", 1)
    assert commented != good and dockerfile_problems(commented) == []
