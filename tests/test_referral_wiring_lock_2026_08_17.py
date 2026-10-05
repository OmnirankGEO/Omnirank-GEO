"""§3 挪家 · referral 域接线锁(原 frontend/scripts/test-referral-wiring.mjs)

## 为什么挪

原锁挂在 `frontend/package.json` 的 `build` 链上,而它要读 `api/referral_api.py`。
Docker 的 frontend-builder 阶段只 `COPY frontend/ ./` —— **后端源码不在那一层**,
于是它每次镜像构建都走 `existsSync` 分支、打印「⏭️ 够不到 …本轮 SKIP」并 `exit 0`。
打印得很大声,可它**从来没有在它该起作用的地方跑过**:
构建绿 ≠ 判据过,而所有人看到的只有构建绿。
(违「引用后端文件的锁不许进前端 build 链」;双包车返修轴清扫点过名。)

## 挪家原则:断言语义逐字保留

下面每一条都对得上原 .mjs 里的同一条,包括两条反向对照和允许清单的过期检查。
唯一变化是**跑在哪儿** —— 后端 pytest 里,前后端源码都够得到,没有 SKIP 分支可走。
原 .mjs 已从 build 链删除(见 frontend/package.json)。
"""

from __future__ import annotations

import io
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_PY = os.path.join(REPO_ROOT, "api", "referral_api.py")
FRONTEND_SRC = os.path.join(REPO_ROOT, "frontend", "src")
PROMO_TSX = os.path.join(FRONTEND_SRC, "pages", "Agent", "PromotionCenter.tsx")

# 允许清单(每条必须写清理由;端点一旦有了调用方,清单条目转红逼着移出)—— 原 .mjs 逐字搬运
ALLOWLIST = {
    "profit-summary": "已被 /api/agent/finance/overview 取代(ProfitDashboard 调后者);端点保留待下线",
}


def _read(path: str) -> str:
    with io.open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _strip_comments(src: str) -> str:
    """与原 .mjs 的 stripComments 同口径:块注释 / 整行 // / 行尾 //(不吃 URL 的 //)。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    src = re.sub(r"([^:'\"])//[^\n]*", r"\1", src)
    return src


def parse_agent_get_endpoints(api_source: str):
    """referral_api.py → 代理开放 GET 端点首段列表(原 parseAgentGetEndpoints 逐字同口径)。"""
    out = []
    for m in re.finditer(r'@router\.get\(\s*"([^"]+)"', api_source):
        path = m.group(1)
        if path.startswith("/api/"):      # 绝对路径的公开端点(share_code 等),不属代理面
            continue
        if path.startswith("/admin"):     # admin 面不在本锁范围
            continue
        seg = path.lstrip("/").split("/")[0]
        if seg and seg not in out:
            out.append(seg)
    return out


def collect_frontend_source() -> str:
    """frontend/src 全量拼接(剥注释)—— 原 collectFrontendSource 同口径。"""
    chunks = []
    for dirpath, _dirnames, filenames in os.walk(FRONTEND_SRC):
        for name in filenames:
            if re.search(r"\.(tsx?|mjs|js)$", name):
                chunks.append(_read(os.path.join(dirpath, name)))
    return _strip_comments("\n".join(chunks))


def check_wiring(endpoints, frontend_blob):
    """原 checkWiring 逐字同口径。返回 (unwired, stale_allowlist)。"""
    unwired, stale = [], []
    for ep in endpoints:
        wired = ("referral/%s" % ep) in frontend_blob
        if not wired and ep not in ALLOWLIST:
            unwired.append(ep)
        if wired and ep in ALLOWLIST:
            stale.append(ep)
    return unwired, stale


# ------------------------------------------------------------ 0. 判据可用性
def test_0_criteria_are_reachable():
    """先证判据不是空的,再谈结论 —— 这正是原锁在 builder 里做不到的那一步。"""
    assert os.path.exists(API_PY), "够不到 %s —— 挪家的意义就是这里必须够得到" % API_PY
    assert os.path.isdir(FRONTEND_SRC), "够不到 frontend/src"
    assert os.path.exists(PROMO_TSX), "够不到 PromotionCenter.tsx"
    endpoints = parse_agent_get_endpoints(_read(API_PY))
    assert len(endpoints) >= 5, "只解析出 %d 个代理面 GET 端点 —— 端点集近乎空,后面全是空即通过" % len(endpoints)


# ------------------------------------------------------------ 1. 接线主判据
def test_1_every_agent_endpoint_has_a_frontend_caller():
    endpoints = parse_agent_get_endpoints(_read(API_PY))
    blob = collect_frontend_source()
    unwired, stale = check_wiring(endpoints, blob)
    problems = ['referral 端点 "/%s" 无任何前端调用方(能力就绪接线没做 · 同型缺口)' % ep for ep in unwired]
    problems += ['"%s" 已有前端调用方,请把它移出允许清单' % ep for ep in stale]
    assert not problems, "\n  · " + "\n  · ".join(problems)


def test_1b_allowlist_keys_are_real_endpoints():
    """允许清单键必须都是真实端点(清单指向不存在的端点 = 清单烂了)。"""
    endpoints = parse_agent_get_endpoints(_read(API_PY))
    stale = [k for k in ALLOWLIST if k not in endpoints]
    assert not stale, '允许清单里的 %s 已不是 referral_api 端点,清单过期' % stale


# ------------------------------------------------------------ 2. 反向对照
def test_2a_reverse_control_fake_endpoint_is_caught():
    """必须命中:注入假端点,检查器必须抓到 —— 证明不是恒真。"""
    endpoints = parse_agent_get_endpoints(_read(API_PY))
    blob = collect_frontend_source()
    unwired, _ = check_wiring(endpoints + ["zzz-fake-endpoint"], blob)
    assert "zzz-fake-endpoint" in unwired, "检查器连注入的假端点都抓不到 —— 判据恒绿,作废"


def test_2b_reverse_control_wired_endpoint_not_misreported():
    """必须不命中:已接线端点(stats)不得被误报 —— 证明不是恒红。"""
    blob = collect_frontend_source()
    unwired, _ = check_wiring(["stats"], blob)
    assert unwired == [], "已接线的 stats 被误报为未接线 —— 判据口径坏了"


# ------------------------------------------------------------ 3. 推广中心名单区
def test_3_promotion_center_team_section():
    promo = _strip_comments(_read(PROMO_TSX))
    problems = []
    if "/api/referral/team" not in promo:
        problems.append("PromotionCenter 未调用 /api/referral/team")
    if "referral-team-section" not in promo:
        problems.append("PromotionCenter 缺下线名单区(referral-team-section)")
    if "还没有人通过你的链接注册" not in promo:
        problems.append("名单空态人话文案缺失")
    if re.search(r"maskMemberName[\s\S]{0,400}\{m\.username\}", promo):
        problems.append("名单区疑似裸渲染 username(手机号),必须走 maskMemberName")
    assert not problems, "\n  · " + "\n  · ".join(problems)


# ------------------------------------------------------------ 4. 挪家自证
def test_4_build_chain_membership_follows_the_rule_not_the_filename():
    """build 链成员资格按**规则**定,不按文件名定(Review-CTO 2026-08-18 裁定)。

    规则只有一条:**引后端文件的锁不许进 build 链**。由此:
      · `test-referral-wiring.mjs` —— 整条判据已搬进本文件,原 .mjs 已删,不许再出现;
      · `test-diagnosis-launch-ui.mjs` —— §1 已挪走、现零后端引用 = **合规**,
        裁定放回 build 链恢复真执行(它那 46 条前端专属判据本来就没病,
        不该被"删两处调用"顺手带走);
      · 越界闸 `verify-no-backend-refs-in-build-chain.mjs` 必须在链上,
        否则规则没人执行,上面这条"合规"随时会悄悄失效。
    """
    pkg = _read(os.path.join(REPO_ROOT, "frontend", "package.json"))
    build_line = [ln for ln in pkg.split("\n") if '"build"' in ln]
    assert build_line, "frontend/package.json 里找不到 build 脚本 —— 判据不可用"
    build = build_line[0]

    assert "test-referral-wiring" not in build, "referral 锁仍挂在 build 链上(挪家没做完)"
    assert "test-diagnosis-launch-ui" in build, (
        "test-diagnosis-launch-ui.mjs 不在 build 链上 —— 它已零后端引用,"
        "按裁定应恢复真执行;不在链上 = 那 46 条前端判据又被静默跳过"
    )
    assert "verify-no-backend-refs-in-build-chain" in build, (
        "越界闸不在 build 链上 —— 规则没人执行,上面那条'合规'随时会悄悄失效"
    )
    # 反向对照:build 链本身非空(否则上面三条是空即通过)
    assert build.count("node scripts/") >= 10, "build 链里几乎没有 node 判据了,上面三条成了空即通过"


def test_4b_gate_runs_before_anything_expensive():
    """越界闸必须排在 `tsc -b` / `vite build` **之前** —— 违规要在几秒内报,不是几分钟后。"""
    import json

    build = json.loads(_read(os.path.join(REPO_ROOT, "frontend", "package.json")))["scripts"]["build"]
    steps = [s.strip() for s in build.split("&&")]
    gate = next((i for i, s in enumerate(steps) if "verify-no-backend-refs-in-build-chain" in s), -1)
    tsc = next((i for i, s in enumerate(steps) if s.startswith("tsc")), len(steps))
    vite = next((i for i, s in enumerate(steps) if s.startswith("vite build")), len(steps))
    assert gate >= 0, "build 链里没有越界闸"
    assert gate < tsc and gate < vite, (
        "越界闸排在第 %d 步,而 tsc 在第 %d / vite 在第 %d —— 应当 fail fast" % (gate, tsc, vite)
    )


# ------------------------------------------------------------ 5. 通用规则闸
def test_5_no_build_chain_script_reaches_outside_frontend():
    """通则闸:`引用后端文件的锁不许进前端 build 链`。

    只钉这两条会漏第三条 —— Docker 的 frontend-builder 阶段只 `COPY frontend/ ./`,
    **任何**伸手到 frontend/ 之外的 build 脚本都会走上同一条路:
    要么整个 build 挂掉(ENOENT),要么加一个 SKIP 分支变成裸奔判据。
    所以判据打在**规则**上,不打在这两个文件名上。
    """
    import json

    pkg = json.loads(_read(os.path.join(REPO_ROOT, "frontend", "package.json")))
    build = pkg["scripts"]["build"]
    scripts = re.findall(r"node (scripts/[\w\-.]+\.mjs)", build)
    assert len(scripts) >= 10, "build 链只解析出 %d 个 node 脚本 —— 判据近乎空即通过" % len(scripts)

    outside = re.compile(
        r"""['"]\.\./(server\.py|api/|db/|services/|tools/|writing/|middleware/"""
        r"""|auth/|agents/|workflows/|config/|scripts/)"""
    )
    # 🔴 先剥注释再判。裸文本判会命中**解释这条规则的注释** ——
    #    build 链里的越界闸自己、以及 test-diagnosis-launch-ui.mjs 的挪家说明,
    #    注释里都写着 `../server.py` 这类字样。2026-08-18 本条就是这么红的。
    #    (JS 侧那条闸一开始就剥了注释,Python 侧这条忘了 —— 同一条规则两处实现,漏了一处。)
    bad = []
    for rel in scripts:
        path = os.path.join(REPO_ROOT, "frontend", rel)
        if not os.path.exists(path):
            bad.append("%s 在 build 链里但文件不存在" % rel)
            continue
        hits = sorted({m.group(0) for m in outside.finditer(_strip_comments(_read(path)))})
        if hits:
            bad.append("%s 伸手到 frontend/ 之外:%s" % (rel, hits))
    assert not bad, "\n  · " + "\n  · ".join(bad)

    # 反向对照①:探测器对**确实越界**的真代码必须命中,否则上面的"0 命中"零判别力
    probe = "const P = path.resolve(root, '../server.py');"
    assert outside.search(_strip_comments(probe)), "越界探测器连合成正样本都抓不到 —— 判据恒绿"
    # 反向对照②:剥注释不许把真代码一起吃掉(否则①是靠"剥光了"过的)
    mixed = "// 注释里的 '../server.py'\nconst REAL = path.resolve(root, '../api/x.py');"
    stripped = _strip_comments(mixed)
    assert "../api/" in stripped and "// 注释里的" not in stripped, \
        "剥注释口径坏了(要么没剥掉注释,要么把真代码也剥了):%r" % stripped


def test_6_no_silent_skip_branch_left_in_build_chain_scripts():
    """build 链脚本里不许再有「够不到就 SKIP」这一形态 —— 它正是本单要根除的病。"""
    import json

    pkg = json.loads(_read(os.path.join(REPO_ROOT, "frontend", "package.json")))
    scripts = re.findall(r"node (scripts/[\w\-.]+\.mjs)", pkg["scripts"]["build"])
    # 同上:剥注释再判。这两句话正是**描述这个病**的说明文字,
    # 越界闸的文档注释和挪家说明里都引用了它们 —— 裸文本判会把讲解当成病灶。
    offenders = []
    for rel in scripts:
        path = os.path.join(REPO_ROOT, "frontend", rel)
        if not os.path.exists(path):
            continue
        src = _strip_comments(_read(path))
        if "本轮 SKIP" in src or "这不是通过,是没跑" in src:
            offenders.append(rel)
    assert not offenders, "build 链里仍有大声 SKIP 的裸奔判据:%s" % offenders

    # 反向对照:真的 SKIP 分支(代码里的 console.log,不是注释)必须被抓到
    real_skip = "if (!existsSync(P)) {\n  console.log('本轮 SKIP');\n  process.exit(0);\n}"
    assert "本轮 SKIP" in _strip_comments(real_skip), "剥注释把真 SKIP 分支也吃掉了 —— 判据恒绿"
    only_comment = "// 说明:以前这里会打印 本轮 SKIP,现已根除"
    assert "本轮 SKIP" not in _strip_comments(only_comment), "注释里的讲解仍被当成病灶"
