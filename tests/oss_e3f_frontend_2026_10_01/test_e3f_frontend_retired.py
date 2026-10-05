"""开源 E3 · 前端(WO_322)· 2026-10-01:删掉的前端域、伴随件、孤儿与路由不许回来。

WO_322 删了 256 个前端文件:七个整目录(社媒工作台 / 旧社媒操盘手 / 旧 C 端 / M3 页面 / M3 组件 / 社媒组件 /
顾问团)+ 目录外 35 个(旧 agent 对话 UI 的伴随件、删完入口后从 html 入口不可达的孤儿、顾问管理页)。
连带删了 App.tsx 里对应的路由(社媒 /s 族除 /s/:token 外、/social*、/social-studio*、/social-ops 整块、
/agent、/advisors 族)。

守三件事(全是静态扫描,不起应用,单包约 1s):
1. 目录与文件都不在(已跟踪 + 磁盘两头都查);
2. frontend/src 与 frontend/tests 下已跟踪的源码里,没有任何 import / export-from / 动态 import() / lazy 的模块说明符
   解析进它们(@/ 别名 + 相对路径都解析;判据的牙证与对照在格内);
3. App.tsx 里不再有那些 Route path(对照臂:在役的 /s/:token 客户选词报价仍在)。
「流向导航的站内路由必须解析到现存路由」是 build 链里 test-route-literals-live.mjs 的 S3,不在本包重复。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[2]

RETIRED_DIRS = [
    "frontend/src/pages/SocialStudio",
    "frontend/src/pages/SocialOperator",
    "frontend/src/pages/CEnd",
    "frontend/src/pages/M3",
    "frontend/src/pages/Advisors",
    "frontend/src/components/m3",
    "frontend/src/components/social",
]
RETIRED_FILES = [
    # 旧 agent 对话 UI 与旧 C 端壳的伴随件(只被删除域使用)
    "frontend/src/components/agent/ActionCard.tsx",
    "frontend/src/components/agent/AgentDrawer.tsx",
    "frontend/src/components/agent/ConfirmCard.tsx",
    "frontend/src/components/agent/LinkCard.tsx",
    "frontend/src/components/agent/MessageList.tsx",
    "frontend/src/components/c_end/CEndLayout.tsx",
    "frontend/src/components/c_end/EmbeddedPanel.tsx",
    "frontend/src/components/layout/ClientContextBar.tsx",
    "frontend/src/pages/Agent/AgentChatPage.tsx",
    # 删完入口后从 html 入口脚本不可达的孤儿
    "frontend/src/components/PersonaInterviewDialog.tsx",
    "frontend/src/components/SpotlightGuide.tsx",
    "frontend/src/components/agent/ABCDPopover.tsx",
    "frontend/src/components/agent/ChatInput.tsx",
    "frontend/src/components/agent/ContextCard.tsx",
    "frontend/src/components/agent/CostEstimateCard.tsx",
    "frontend/src/components/agent/GeoPlanCard.tsx",
    "frontend/src/components/agent/HookListCard.tsx",
    "frontend/src/components/agent/ManagedBrandPlanCard.tsx",
    "frontend/src/components/agent/ManagedCampaignsListCard.tsx",
    "frontend/src/components/agent/ManagedPlanCard.tsx",
    "frontend/src/components/agent/ProfileGrowthCard.tsx",
    "frontend/src/components/agent/QuickActions.tsx",
    "frontend/src/components/agent/Reasoning.tsx",
    "frontend/src/components/agent/ScriptCard.tsx",
    "frontend/src/components/agent/SelectCard.tsx",
    "frontend/src/components/agent/SessionSidebar.tsx",
    "frontend/src/components/agent/StepProgress.tsx",
    "frontend/src/components/agent/TopicListCard.tsx",
    "frontend/src/context/SocialContext.tsx",
    "frontend/src/data/creatorTestQuestions.ts",
    "frontend/src/hooks/useAgentChat.ts",
    "frontend/src/hooks/useAssistantName.ts",
    "frontend/src/hooks/useOnboardingGuide.ts",
    "frontend/src/utils/cendNavigate.ts",
    # 顾问团前端(Review 10-02 范围 A):顾问管理页只有 /advisors/manage 一条路由能到
    "frontend/src/pages/Employees/AdvisorTeam.tsx",
]
RETIRED_ROUTES = [
    "/s", "/s/*", "/s/login", "/s/wallet", "/s/approval/:token", "/s/match/:shareCode",
    "/social", "/social/*", "/social/login", "/social/approval/:token",
    "/social-studio/*", "/social-studio/login", "/social-ops",
    "agent", "advisors", "advisors/manage", "advisors/chat/:advisorId",
    "advisor-market", "advisor-market/chat/:advisorId",
]
LIVE_ROUTE_CONTROL = "/s/:token"

_SPEC = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(\s*|\bimport\s+|\brequire\s*\(\s*)['"]([^'"]+)['"]""")
_EXTS = ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js")
_RETIRED_STEMS = sorted({str(PurePosixPath(f).with_suffix("")) for f in RETIRED_FILES})


def _tracked(prefix: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", prefix], cwd=str(REPO), capture_output=True, check=True).stdout
    return [x for x in out.decode("utf-8").split("\0") if x]


def _resolve_base(src: str, spec: str) -> str | None:
    """模块说明符 → 仓内路径(不补扩展名);非本仓别名 / 相对路径 ⇒ None。"""
    if spec.startswith("@/"):
        return "frontend/src/" + spec[2:]
    if spec.startswith("."):
        parts: list[str] = []
        for p in str(PurePosixPath(src).parent / spec).split("/"):
            if p == "..":
                if parts:
                    parts.pop()
            elif p and p != ".":
                parts.append(p)
        return "/".join(parts)
    return None


def _hits_retired(base: str) -> bool:
    if any(base == d or base.startswith(d + "/") for d in RETIRED_DIRS):
        return True
    return any(base + e in RETIRED_FILES for e in _EXTS) or base in _RETIRED_STEMS


def _retired_imports(sources: dict[str, str]) -> list[str]:
    bad = []
    for path, text in sources.items():
        for m in _SPEC.finditer(text):
            base = _resolve_base(path, m.group(1))
            if base and _hits_retired(base):
                bad.append(f"{path} → {m.group(1)}")
    return bad


def test_retired_dirs_and_files_are_gone():
    for d in RETIRED_DIRS:
        assert not _tracked(d), f"{d} 下还有已跟踪文件"
        assert not (REPO / d).exists(), f"{d} 在磁盘上还在"
    for f in RETIRED_FILES:
        assert not _tracked(f), f
        assert not (REPO / f).exists(), f
    assert len(RETIRED_DIRS) == 7 and len(RETIRED_FILES) == 35  # 尺子自证:清单没被悄悄改短


def test_no_frontend_module_imports_a_retired_path():
    # 分母 = 运行时源码 + 浏览器 spec。frontend/scripts 不进:判据脚本里有**故意写的**毒与合成源
    #   (WO_260 毒跑器 P3、结构闸 C6 都写着 `@/components/m3/parts`),它们正是用来证明扫描器有牙的。
    files = [f for f in _tracked("frontend/src") + _tracked("frontend/tests") if re.search(r"\.(tsx?|jsx?|mjs|cjs)$", f)]
    assert len(files) > 500, len(files)  # 分母自证:真扫到了前端源码
    sources = {f: (REPO / f).read_text(encoding="utf-8", errors="replace") for f in files}
    assert _retired_imports(sources) == []


def test_import_scanner_has_teeth_and_a_control():
    """牙证:四种写法 × 三类目标都抓到;对照:在役模块的同形写法不抓。"""
    poison = {
        "frontend/src/pages/Brand/X.tsx": (
            "import A from '@/pages/SocialStudio/SocialStudioApp';\n"
            "const B = lazy(() => import('@/components/m3/layout/M3Layout'));\n"
            "export { C } from '../../hooks/useAgentChat';\n"
        ),
        "frontend/src/context/Y.tsx": "import { useSocial } from './SocialContext';\n",
    }
    got = _retired_imports(poison)
    assert len(got) == 4, got
    control = {
        "frontend/src/pages/Brand/X.tsx": (
            "import A from '@/pages/Selection/SelectionPage';\n"
            "const B = lazy(() => import('@/components/workbench/parts'));\n"
            "export { C } from '../../hooks/useWhitelabel';\n"
        ),
        "frontend/src/context/Y.tsx": "import { useAuth } from './AuthContext';\n",
    }
    assert _retired_imports(control) == []


def test_retired_routes_are_gone_from_app_and_a_live_one_remains():
    app = (REPO / "frontend/src/App.tsx").read_text(encoding="utf-8")
    paths = set(re.findall(r'<Route\s+path="([^"]+)"', app))
    assert len(paths) > 100, len(paths)  # 分母自证
    back = sorted(set(RETIRED_ROUTES) & paths)
    assert back == [], back
    assert LIVE_ROUTE_CONTROL in paths, "对照臂:在役的客户选词报价 /s/:token 必须还在(扫描器看得见 App.tsx)"
