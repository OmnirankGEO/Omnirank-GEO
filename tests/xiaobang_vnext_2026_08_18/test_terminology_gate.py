"""术语门 · **语义域锁**(不是禁词锁)。

裁定来源:仓内签发物 ``docs/xiaobang/vnext/RULING_TERMINOLOGY_EDU_2026-08-18.md``
(与 `C:\\AI-Test\\RULING_TERMINOLOGY_EDU_2026-08-18.md` 逐字节相同)。
判据**不引用对话、不引用 docstring 自述** —— 见
:func:`test_ruling_is_vendored_and_fingerprint_matches`。

## 为什么从禁词锁改成语义域锁

R2 的禁词锁把「额度」当裸词禁,产出两个问题:

1. **误伤 ④ 域**:``agent-settlement.md`` 的「同一笔收益剩余额度」被改成「剩余算力」
   —— 那是**钱**,不是算力,资金语义错误(P1-1)。裸词禁不但抓不到这个错,
   它本身就是诱因:它只要求「额度」消失,不管换成什么。
2. **误禁 ③ 域**:裁定明写「额度」在上限/名额语境「不推荐但不算错义」,
   裸禁会把合法表述判红,而长期误报的门最后会被人关掉。

所以锁分成两把,方向相反:

* :func:`terminology_domains.domain12_violations` —— 该改没改;
* :func:`terminology_domains.cash_semantic_violations` —— **不该改却改了**。

第二把是裁定点名要的「独立的资金语义判据」:禁词锁只证明「改了」,
证明不了「改对了」。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from . import terminology_domains as dom
from . import kb_index_sources as idx

# ── 扫描面 ────────────────────────────────────────────────────────────────
# 🔴 R3-P6 制度修复:分母不再手写文件清单,改从**索引结构锚**枚举。
#    R3 的手写清单四个条目全清干净、锁全绿,上线当天小榜照样答
#    「充值积分」「设置额度」—— 因为真正进索引的
#    `api/help_docs_content.json` 与 `faq_items`(种子在 db/faq_db.py)
#    一条都不在清单里。清单不是分母。理由与四道完备性闸见 kb_index_sources。
_ARCHIVE_ALLOWLIST = tuple(path for path, _reason in idx.ARCHIVE_ALLOWLIST)

#: 前端已清理的 GEO 域文件(判据打**渲染行**;注释与 JSX 注释不属用户可见文案)。
_FRONTEND_CLEANED = (
    "frontend/src/pages/Customer/CreditWallet.tsx",
    "frontend/src/pages/Wallet/WalletPage.tsx",
    "frontend/src/context/WalletContext.tsx",
    "frontend/src/pages/Agent/ProcurementSSOT.tsx",
    "frontend/src/lib/v35Terminology.ts",
    "frontend/src/pages/Organization/OrganizationCenter.tsx",
    "frontend/src/pages/Admin/DiagnosisFundExceptions.tsx",
    "frontend/src/pages/Admin/FinanceCenter.tsx",
    "frontend/src/components/gapPlan/GapPlanExecutionView.tsx",
    "frontend/src/pages/Quote/utils/priceRationale.ts",
)
_FRONTEND_OUT_OF_SCOPE = {
    # [开源 E3 · 前端 · 2026-10-01 · WO_322] 社媒工作台页面目录随开源 E3 整删,这条豁免没有对象了
    "frontend/src/components/subscription/": "月卡/订阅是社媒 IP 变现面,同上",
    "frontend/src/sandbox/data/demoReport.md": "demo 正文的「保险额度」属裁定 ④ 域,禁止替换",
}

_FORBIDDEN_ENTRY = "写作大厅"
_CONVERSION = re.compile(
    r"(\d[\d,\.]*\s*元\s*=\s*\d)|((算力|积分)\s*[÷/]\s*\d)|(\d\s*折出厂)"
)


def _scan_files() -> list[Path]:
    return idx.scan_paths()


def _rendered_lines(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        b = line.strip()
        if b.startswith(("//", "*", "/*", "#", "{/*")):
            continue
        out.append(line)
    return out


# ── 裁定来源 ──────────────────────────────────────────────────────────────
def test_ruling_is_vendored_and_fingerprint_matches():
    """判据必须能在 fresh worktree 里解析到裁定来源。

    裁定原文放在 `C:\\AI-Test\\` 下、不在仓里 —— 那样 CI 与新 worktree 读不到,
    「引用签发物」就只是一句话。所以把它逐字节 vendor 进仓,并核内容 sha256。
    """
    assert dom.RULING_PATH.exists(), dom.RULING_PATH
    actual = hashlib.sha256(dom.RULING_PATH.read_bytes()).hexdigest()
    assert actual == dom.RULING_SHA256, (actual, dom.RULING_SHA256)
    text = dom.RULING_PATH.read_text(encoding="utf-8")
    # 四个域必须都能在签发物里解析到 —— 分类器的域划分不是自己发明的。
    for marker in ("① 计价单位", "② 算力池命名", "③ 容量/权限配额", "④"):
        assert marker in text, marker
    assert "禁止替换" in text, "④ 域的处置词丢了"


# ── ④ 资金语义判据(裁定点名要求)───────────────────────────────────────
_CASH_POSITIVES = (
    # 🔴 裁定原话的样本:「一句『剩余收益可分次申请』被改成『剩余算力』→ 必须红」
    "提现按先结算先提锁定对应收益明细，同一笔收益剩余算力可分次申请。",
    "本笔收益的全部算力已冻结，等待打款。",
    "本次保险算力不足",
    "毛利算力偏低，请核对定价。",
    "本次交付算力 3 条",
    "还有 5 篇算力没有安排。",
)

_CASH_NEGATIVES = (
    "佣金算力满 ¥100（13000 算力）可提现到银行卡",   # 佣金本就以算力入账,合法池名
    "赠送算力可消费、不可提现",
    "本期对应算力的出厂成本（服务商进货价）",
    "同一笔收益的剩余金额可分次申请",                 # 修好之后的正确写法
    "按其充值金额一定比例（15%）折算的赠送算力返利",
    "客户当前剩余算力自动取小值",
)


@pytest.mark.parametrize("sample", _CASH_POSITIVES, ids=lambda s: s[:14])
def test_cash_semantic_positive_samples_are_red(sample):
    """🔴 构造「收益写成算力」正样本 → 必须红。

    这条是 P1-1 的返工核心:禁词锁证明不了改对,只证明改了。
    """
    assert dom.cash_semantic_violations(sample), sample


@pytest.mark.parametrize("sample", _CASH_NEGATIVES, ids=lambda s: s[:14])
def test_cash_semantic_negative_samples_stay_green(sample):
    """反向:合法算力表述不许被判红,否则这把锁会因误报被关掉。"""
    assert dom.cash_semantic_violations(sample) == [], sample


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: p.name)
def test_kb_has_no_cash_semantic_error(path: Path):
    problems = dom.cash_semantic_violations(path.read_text(encoding="utf-8"))
    assert not problems, "{0}:{1}".format(path, problems[:3])


@pytest.mark.parametrize("relpath", _FRONTEND_CLEANED, ids=lambda p: p.rsplit("/", 1)[-1])
def test_frontend_has_no_cash_semantic_error(relpath: str):
    text = "\n".join(_rendered_lines(Path(relpath)))
    problems = dom.cash_semantic_violations(text)
    assert not problems, "{0}:{1}".format(relpath, problems[:3])


# ── 已废制度 / 具体资金比率(R3 · Review 2026-08-18 指令)────────────────
_DEAD_POSITIVES = (
    "来源含直推 28%、间推 5%",
    "标「直推 28%」或「间推 5%」",
    "来自推荐分成，直推 28%、间推 5%",
    "满 ¥100（13000 算力）可提现到银行卡",
    "手续费 1%，实际到账 = 金额 − 1%",
    # 🔴 M15 变异存活暴露的缺口:R3-P5 的锁只写了「手续费 5%」这一种语序,
    #    而 FAQ seed 里真正的写法是**数字在前**的「扣 5% 手续费」。
    #    加宽了 pattern 却没加这条正样本 = 加宽本身没有判据在守。
    "原路退回未消耗部分 · 扣 5% 手续费 · 3-7 个工作日到账",
    "充值赠送比例 20%，到账后可消费",
    "转换时系统会多送约 20%",
    "按金额有阶梯赠送（约 10%～30%）",
    # R3-P2 ③:referral 的写死返利率(SSOT 已裁,Owner 可否决)
    "你即时获得按其充值金额一定比例（15%）折算的赠送算力返利",
    "15% 算力返利（可消费）",
    "返利率（15%）",
)
#: 🔴 最要紧的一条反向对照在这里:改后文案**仍然会提「直推收益/间推收益」**——
#:    因为 UI 对历史行就是这么渲染的,KB 必须能解释那个标签。
#:    锁的是**带比率的制度描述**,不是这两个词本身;锁错了会逼下一个人把
#:    「那是旧标签」这句解释也删掉,用户看见标签而助手说没这回事。
_DEAD_NEGATIVES = (
    "历史记录上可能还标着「直推收益 / 间推收益」，那是旧制度留下的标签，不代表现在还有这项分成",
    "按固定比例的多级推荐分成制度已于 2026-05-30 停止，不再产生新的分成",
    "佣金算力达到页面显示的最低提现金额、已实名、已绑卡时才出现在佣金卡里",
    "手续费与实际到账由页面按当前费率实时算出并显示",
    "不同金额档位的赠送比例不同，到账算力以充值页显示为准",
    "转换加成以转换弹窗显示为准",
    "按其充值金额一定比例折算的赠送算力返利（比例以页面显示为准）",
    # 🔴 这两条是**区间/校验**语境:服务商自己填的返利比例输入框取值范围,
    #    不是平台背了一个固定比率。第一版的返利锁把它们判红了 ——
    #    锁太宽会逼人删掉合法说明,最后门被关掉。
    "填返利比例（0~100%）",
    "返利比例填了超过 100% → 提示返利比例须在 0 ~ 100% 之间",
)


@pytest.mark.parametrize("sample", _DEAD_POSITIVES, ids=lambda s: s[:14])
def test_dead_scheme_positive_samples_are_red(sample):
    """已废的多级分成比率、以及任何写死的资金比率 → 必须红。

    生产小榜此前一直照 KB 讲解 28%/5% 这套 **2026-05-30 已废**的制度
    (在码实证 `frontend/src/pages/Agent/ProfitDashboard.tsx:4`)。
    这是**存量病** —— 不是本包引入的,但本包接手时它就在用户面前活着。
    """
    assert dom.dead_scheme_violations(sample), sample


@pytest.mark.parametrize("sample", _DEAD_NEGATIVES, ids=lambda s: s[:14])
def test_dead_scheme_negative_samples_stay_green(sample):
    assert dom.dead_scheme_violations(sample) == [], sample


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: p.name)
def test_kb_has_no_dead_scheme_or_hardcoded_rate(path: Path):
    problems = dom.dead_scheme_violations(path.read_text(encoding="utf-8"))
    assert not problems, "{0}:{1}".format(path, problems[:3])


def test_the_dead_scheme_lock_bans_rates_not_the_words():
    """锁的**形状**:「直推」「间推」「手续费」这些词本身不禁,禁的是它们带比率。"""
    for word_only in ("直推收益", "间推收益", "手续费与实际到账由页面实时算出", "推荐分成已停止"):
        assert dom.dead_scheme_violations(word_only) == [], word_only


# ── ①② 语义域锁 ─────────────────────────────────────────────────────────
_D12_POSITIVES = ("工具额度", "本次消耗 130 额度", "当前功能可用额度不足",
                  "到账额度 5000", "额度包售价", "赠送额度从赠送库存扣")
#: ③ 与 ④ 域的合法表述 —— 一条都不许被判红。
_D12_NEGATIVES = ("接口调用额度", "审批额度", "每月固定额度", "本月剩余额度上限",
                  "保险额度不足", "算力上限用满时自动使用老板钱包", "名额已满",
                  "同一笔收益的剩余金额可分次申请")


@pytest.mark.parametrize("sample", _D12_POSITIVES, ids=lambda s: s[:12])
def test_domain12_positive_samples_are_red(sample):
    assert dom.domain12_violations(sample), sample


@pytest.mark.parametrize("sample", _D12_NEGATIVES, ids=lambda s: s[:12])
def test_domain12_negative_samples_stay_green(sample):
    """③「不推荐但不算错义」、④「禁止替换」—— 两域都不该被 ①② 锁碰。"""
    assert dom.domain12_violations(sample) == [], sample


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: p.name)
def test_kb_has_no_domain12_residue(path: Path):
    problems = dom.domain12_violations(path.read_text(encoding="utf-8"))
    assert not problems, "{0}:{1}".format(path, problems[:3])


@pytest.mark.parametrize("relpath", _FRONTEND_CLEANED, ids=lambda p: p.rsplit("/", 1)[-1])
def test_frontend_has_no_domain12_residue(relpath: str):
    text = "\n".join(_rendered_lines(Path(relpath)))
    problems = dom.domain12_violations(text)
    assert not problems, "{0}:{1}".format(relpath, problems[:3])


def test_the_lock_is_not_a_bare_word_ban():
    """锁的**形状**判据:裁定明令「不做全文裸词禁」。

    如果哪天有人把它退回成 ``'额度' not in text``,③ 域那批合法表述会全红,
    这条会先响。
    """
    for legitimate in ("接口调用额度", "审批额度", "每月固定额度", "保险额度不足"):
        assert "额度" in legitimate                        # 确实含裸词
        assert dom.domain12_violations(legitimate) == []   # 却不该红
        assert dom.cash_semantic_violations(legitimate) == []


# ── 旧入口名与固定换算公式(与语义域无关,仍是硬错)─────────────────────
@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: p.name)
def test_no_stale_entry_name_or_legacy_unit(path: Path):
    text = path.read_text(encoding="utf-8")
    assert _FORBIDDEN_ENTRY not in text, "{0}:仍含旧入口名「写作大厅」".format(path)
    # 「积分」在本仓不存在合法语义域(DP-B4 明令),故仍按裸词禁。
    assert "积分" not in text, "{0}:仍含旧计价单位「积分」".format(path)


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: p.name)
def test_no_hardcoded_conversion_formula(path: Path):
    text = path.read_text(encoding="utf-8")
    hit = _CONVERSION.search(text)
    assert hit is None, "{0}:仍含固定换算公式 {1!r}".format(path, hit.group(0) if hit else "")


# ── KB 索引口径残留门(R3-P6 · 34 班 verify ④ 挂的账)───────────────────
#: 线上口径:``kb_chunks`` 含「积分」=0 且含「额度」=0。
#: ③ 域在这一层也要清 —— 裁定 ③ 给的推荐写法就是「算力上限/名额」,
#: 而现役组织中心 UI 已经整屏说「算力上限」。KB 讲屏幕上没有的词 = 用户找不到入口。
_KB_RESIDUE_POSITIVES = (
    "充进来的是充值积分",
    "失败会自动把冻结的积分原路退回",
    "去写作大厅生成文章",
    # ② 域池名:含现金词也照样红(R3-P8 ④ 的次序修复)
    "佣金额度不足",
    "到账额度不足",
)
#: 🔴 [R3-P9 ⑤] ③ 域**裸词**已从硬门降为 advisory。
#:
#:    原来这两条在 ``_KB_RESIDUE_POSITIVES`` 里(硬门必红)。Review-CTO 裁定:
#:    门比它所依据的签发规则更严 —— 裁定 ③ 的原话是「不推荐但**不算错义**」——
#:    会让写文案的人被一条"规则里查不到"的红拦住,他去查裁定发现这不算错,
#:    下一步不是改文案而是**把门关掉**。
#:    所以降级为 warn。要更严的话正确做法是提请升裁定的规则版本,不是让门私自加严。
_KB_ADVISORY_SAMPLES = (
    "团队员工使用的是老板授权的团队额度",
    "全部客户、分配权限、设置额度、承担付款",
)
#: 🔴 **最要紧的反向对照**:④ 域必须放行。
#:    WO 2026-08-19 原话「判据『额度=0』不许用错义替换凑出来」——
#:    如果这把锁把「保险额度」也判红,下一个人就会把它改成「保险算力」,
#:    那正是 P1-1 那类资金语义事故。所以 ④ 在这里也豁免。
_KB_RESIDUE_NEGATIVES = (
    "本次保险额度不足",
    "同一笔收益的剩余金额可分次申请",
    "老板设置的算力上限",
    "每词 130 算力",
    "提现手续费与到账时间以页面显示为准",
)


@pytest.mark.parametrize("sample", _KB_RESIDUE_POSITIVES, ids=lambda s: s[:14])
def test_kb_residue_positive_samples_are_red(sample):
    assert dom.kb_index_residue(sample), sample


@pytest.mark.parametrize("sample", _KB_RESIDUE_NEGATIVES, ids=lambda s: s[:14])
def test_kb_residue_negative_samples_stay_green(sample):
    assert dom.kb_index_residue(sample) == [], sample


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: p.name)
def test_index_source_has_no_legacy_residue(path: Path):
    """分母里的每一份内容源:零「积分」、零非现金域「额度」、零旧入口名。"""
    problems = dom.kb_index_residue(path.read_text(encoding="utf-8"))
    assert not problems, "{0}:{1}".format(path, problems[:3])


def test_kb_residue_gate_cannot_be_satisfied_by_cash_mistranslation():
    """两把锁配对:靠错义替换把「额度」清成 0 的那条路必须走不通。

    「本次保险额度不足」→ 残留门放行(④ 豁免);
    有人硬把它改成「本次保险算力不足」→ 资金语义判据当场判红。
    """
    before = "本次保险额度不足"
    after = "本次保险算力不足"
    assert dom.kb_index_residue(before) == []
    assert dom.cash_semantic_violations(before) == []
    assert dom.kb_index_residue(after) == []          # 残留门看不见这个错
    assert dom.cash_semantic_violations(after)        # 但另一把锁看得见


# ── 分母与豁免自证 ────────────────────────────────────────────────────────
def test_scan_scope_is_not_empty():
    assert len(_scan_files()) >= 28, [str(f) for f in _scan_files()]


def test_scan_scope_contains_the_two_sources_r3_missed():
    """🔴 回归钉:R3 分母漏的就是这两个,线上 15 条残留全出自它们。

    不断言"清单里有这两行"(那还是清单),断言的是**结构锚解析出来的分母**
    里有它们 —— 改 builder 的路径常量,这条会跟着变。
    """
    scanned = {p.as_posix() for p in _scan_files()}
    assert "api/help_docs_content.json" in scanned, sorted(scanned)
    assert "db/faq_db.py" in scanned, sorted(scanned)


def test_frontend_scope_yields_real_lines():
    for relpath in _FRONTEND_CLEANED:
        assert _rendered_lines(Path(relpath)), relpath


def test_out_of_scope_list_is_explicit_and_reasoned():
    for path, reason in _FRONTEND_OUT_OF_SCOPE.items():
        assert reason.strip(), path
        assert Path(path).exists(), path


def test_archive_allowlist_is_explicit_and_still_exists():
    for path in _ARCHIVE_ALLOWLIST:
        assert Path(path).exists(), path
    scanned = {str(p).replace("\\", "/") for p in _scan_files()}
    for path in _ARCHIVE_ALLOWLIST:
        assert path not in scanned, "{0} 既在白名单又在扫描集里".format(path)


def test_conversion_formula_gate_is_alive():
    for poison in ("1 元 = 130 积分", "1元=144.4 出厂算力", "算力 ÷ 130", "9 折出厂"):
        assert _CONVERSION.search(poison), poison
    for ok in ("本次消耗 130 算力", "换算系数由平台后台配置", "本次交付 3 条"):
        assert _CONVERSION.search(ok) is None, ok


# ── ⑤ 与现役 UI 用词对齐(R3-P7 ⑤)─────────────────────────────────────
#: 员工算力上限这件事,现役组织中心 UI 只说一个词。KB 说别的词 = 用户照着找不到。
#: 🔴 判据打的是**UI 里真实出现的词**,不是我记得的词 —— 所以先从 UI 源码取真值,
#:    再要求 KB 侧与它一致。UI 改了词,这条会先红,而不是等用户来投诉。
_ORG_UI_SOURCE = Path("frontend/src/pages/Organization/OrganizationCenter.tsx")

#: KB 里**不许**出现的员工额度类说法(它们在现役 UI 里一次都没有)。
_SEAT_LIMIT_STALE_PHRASINGS = (
    "员工共享算力", "团队额度", "团队使用额度", "员工额度", "共享额度",
)


def test_live_ui_really_uses_the_word_we_align_to():
    """先证明对齐目标存在:UI 里「算力上限」必须真的出现。

    不做这一步,下面那条就是拿一个我脑补的词去要求 KB —— 而 UI 可能压根不这么说。
    """
    ui = _ORG_UI_SOURCE.read_text(encoding="utf-8")
    assert ui.count("算力上限") >= 5, ui.count("算力上限")
    # 反向对照:UI 里确实**没有**那批旧说法(否则 KB 该跟 UI 一致地保留它们)。
    for phrase in _SEAT_LIMIT_STALE_PHRASINGS:
        assert phrase not in ui, "UI 其实在用「{0}」,KB 侧的判据要跟着改".format(phrase)


@pytest.mark.parametrize("path", _scan_files(), ids=lambda p: p.name)
def test_index_sources_use_the_same_seat_limit_wording_as_the_ui(path: Path):
    """KB 索引源里不许出现现役 UI 不用的员工额度类说法。"""
    text = path.read_text(encoding="utf-8")
    hits = [phrase for phrase in _SEAT_LIMIT_STALE_PHRASINGS if phrase in text]
    assert not hits, "{0}:UI 不用这些说法 {1},应统一为「算力上限」".format(path, hits)


def test_the_ui_parity_lock_is_alive():
    """活性自证:这把锁必须真的能抓到那批说法。"""
    for phrase in _SEAT_LIMIT_STALE_PHRASINGS:
        assert phrase in "客户消费、{0}和服务商库存应分别记账".format(phrase)
    # 而正确说法不许被判红。
    ok = "客户消费、员工算力上限和服务商库存应分别记账"
    assert not [p for p in _SEAT_LIMIT_STALE_PHRASINGS if p in ok]


# ── ④ ②/④ 域优先级(R3-P8 · Review 勘误随包落)───────────────────────────
#: 🔴 工单点名的两条反向样本。它们的共同点:**池名里恰好含现金词**。
#:    按「④ 出现即豁免」的老读法它们被放行,而它们其实都该改。
_POOL_BEATS_CASH = ("佣金额度不足", "到账额度不足",
                    "佣金额度已用完,请充值", "本次到账额度 5000")
#: 反向:没命中池名的现金语境仍然豁免 —— 否则就会逼出「保险额度→保险算力」那类错义。
_CASH_STILL_EXEMPT = ("本次保险额度不足", "同一笔收益的剩余金额可分次申请",
                      "提现手续费按页面显示的费率算")


@pytest.mark.parametrize("sample", _POOL_BEATS_CASH, ids=lambda s: s[:12])
def test_pool_name_beats_the_cash_exemption(sample):
    """② 域池名判定**先于** ④ 域现金锚豁免。

    池名是**我们自己给算力池起的名字**,含不含现金词都不改变「它是算力池」;
    ④ 域保护的是「别把别人的钱说成算力」。两者只在「池名恰好含现金词」这一种
    写法上撞车,而那种写法本身就该改。
    """
    assert dom.kb_index_residue(sample), sample


@pytest.mark.parametrize("sample", _CASH_STILL_EXEMPT, ids=lambda s: s[:12])
def test_cash_context_without_a_pool_name_stays_exempt(sample):
    """🔁 反向对照:改次序**不许**把 ④ 域一起收进来。

    收进来的后果就是 P1-1 那类事故:有人为了让计数归零把「保险额度」改成
    「保险算力」——把钱说成了算力。
    """
    assert dom.kb_index_residue(sample) == [], sample


def test_ruling_documents_the_pool_name_precedence():
    """判据的依据必须在**签发物**里可解析,不是在代码注释里自述。

    这条勘误是 Review-CTO 2026-08-19 随本包落的;裁定文件的内容指纹由
    :func:`test_ruling_is_vendored_and_fingerprint_matches` 另行核对,
    所以「勘误在不在」和「文件有没有被换掉」是两条独立判据。
    """
    text = dom.RULING_PATH.read_text(encoding="utf-8")
    assert "②④ 域优先级" in text
    assert "② 域池名判定优先于 ④ 域现金锚点豁免" in text
    # 勘误点名的两条反向样本必须写在签发物里 —— 判据与签发物同源。
    assert "佣金额度不足" in text and "到账额度不足" in text
    assert "保险额度不足" in text


def test_rule_id_and_version_are_parseable_from_the_ruling():
    """⑤ 的错误合同要下发 rule_id/version;两者必须能在签发物里核到。"""
    text = dom.RULING_PATH.read_text(encoding="utf-8")
    from services.kb_terminology_gate import RULING_RULE_ID, RULING_VERSION

    assert RULING_RULE_ID in text, RULING_RULE_ID
    assert RULING_VERSION in text, RULING_VERSION


# ══════════════════════════════════════════════════════════════════════════
# [R3-P9 ⑤] ③ 域裸词 = advisory(warn 不 block)· 门不许比签发规则严
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("sample", _KB_ADVISORY_SAMPLES, ids=lambda s: s[:14])
def test_domain3_bare_word_is_advisory_not_blocking(sample):
    """③ 域裸词:**不拦**(硬门清单里没有它),但**有提醒**(advisory 里有)。

    两半都要打。只验「不拦」的话,把 advisory 一起删掉也照样绿 ——
    那就从「降级」变成了「删掉」,而降级不等于无所谓。
    """
    assert dom.kb_index_residue(sample) == [], (sample, dom.kb_index_residue(sample))
    assert dom.kb_index_advisories(sample), "降成 advisory 之后连提醒都没有了"


@pytest.mark.parametrize("sample", _KB_ADVISORY_SAMPLES, ids=lambda s: s[:14])
def test_advisory_samples_do_not_leak_into_the_write_gate(sample):
    """写入路径的全量判据里也不能有它 —— 否则"降级"只降了一半,
    KB 重建照样被它拦住。
    """
    assert dom.kb_write_violations(sample) == [], sample


def test_pool_names_are_still_hard_blocked_after_the_downgrade():
    """🔁 配对反向:降级只降 ③ 域**裸词**,② 域池名一个都不许松。

    没有这条,「把 ③ 降级」很容易被实现成「把整个 额度 分支删掉」。
    """
    for name in dom.POOL_NAMES:
        clause = name + '不足'
        assert dom.kb_index_residue(clause), name
        assert dom.kb_write_violations(clause), name


def test_cash_domain_is_still_exempt_after_the_downgrade():
    """🔁 另一侧配对:④ 域仍然豁免,且不许靠错义替换把计数凑成 0。"""
    assert dom.kb_index_residue("本次保险额度不足") == []
    assert dom.kb_index_advisories("本次保险额度不足") == []
    # 把它改成「保险算力」→ 资金语义锁当场判红(两把锁方向相反,必须配对)
    assert dom.cash_semantic_violations("本次保险算力不足")


def test_advisories_are_logged_not_raised():
    """advisory 走日志,不走异常 —— 这条钉的是"写入路径不会因为它而失败"。"""
    import logging

    from services.kb_terminology_gate import assert_kb_text_clean

    sample = _KB_ADVISORY_SAMPLES[0]
    records = []

    class _Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("GEO-KbTerminology")
    handler = _Handler()
    logger.addHandler(handler)
    try:
        assert_kb_text_clean(sample, where='p9-advisory-probe')   # 不抛
    finally:
        logger.removeHandler(handler)
    assert any("p9-advisory-probe" in msg for msg in records), records


# ══════════════════════════════════════════════════════════════════════════
# [R3-P9 ⑧] managed_campaign_api 死池名文案(GEO 独占域 · 共享主人翁制)
# ══════════════════════════════════════════════════════════════════════════


_MANAGED_API = Path(__file__).resolve().parents[2] / 'api' / 'managed_campaign_api.py'


def _user_visible_strings(path: Path) -> list[str]:
    """取文件里的**字符串字面量**,排除模块 docstring。

    🔴 不能整文件 grep:越域说明本身要写清楚「改掉的是哪几个词」
    于是模块 docstring 里**必然**出现那些词 —— 拿裸串锚去扫会把说明自己判红
    (本仓 08-19 已经踩过一次)。所以用 AST 取字面量,并显式跳过 docstring。
    """
    import ast

    tree = ast.parse(path.read_text(encoding='utf-8'))
    # docstring 节点按**对象身份**收集,不按文本比对 ——
    # `ast.get_docstring()` 会 cleandoc(去缩进/首尾空行),拿它去 `==` 原始
    # Constant 永远对不上(本轮实测:模块 docstring 因此漏网,锁当场判红了越域说明自己)。
    docstrings = set()
    for holder in [tree] + [n for n in ast.walk(tree) if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]:
        body = getattr(holder, 'body', None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append(''.join(
                part.value for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)))
    return out


def test_managed_campaign_api_has_no_dead_pool_name():
    """🔴 ⑧:「充值额度 / 工具额度 / 积分」→ 算力口径。

    这个文件属 GEO AI 独占域,走共享主人翁制:只改文案、保兼容、
    commit 说明写明越域原因、AI_COORDINATION_LOG 通告。
    """
    assert _MANAGED_API.exists(), _MANAGED_API
    problems = []
    for text in _user_visible_strings(_MANAGED_API):
        hits = dom.kb_index_residue(text)
        if hits:
            problems.append((text[:40], hits[:2]))
    assert not problems, problems


def test_the_managed_campaign_lock_would_have_caught_the_old_copy():
    """🔁 反向样本:改之前的那两句原文,拿**同一把锁**去判必须红。

    没有这条,上面那条「扫完没问题」可能只是因为锁根本扫不到东西
    (取字面量的函数写错、路径写错、AST 走空)。
    """
    before = (
        "充值额度不足,需要 {deduct_points} 工具额度",
        "充值积分不足(并发竞争)",
    )
    for sample in before:
        assert dom.kb_index_residue(sample), sample

    # 分母自证:取字面量的函数**两个分支都要真的取到东西**。
    # 🔴 第一版这里只写 `len(...) > 20` —— 变异实验当场证明它没有判别力:
    #    把 Constant 分支整个跳过,光靠 JoinedStr(f-string)分支也能凑够 20 条,
    #    于是"锁扫得到东西"这句话是假的。附赠项让判据零判别力,得逐分支钉。
    collected = _user_visible_strings(_MANAGED_API)
    assert any(text == "INSUFFICIENT_PAID_POINTS" for text in collected), (
        "Constant(普通字符串字面量)分支没取到东西")
    # 🔴 第二版这里写的是 `"需要" in text and "算力" in text` —— **同样没有判别力**:
    #    f-string 的字面量片段本身会被 ast.walk 当普通 Constant 遍历到,
    #    而片段「充值算力不足,需要 」正好两个词都占全了。要打的是**拼接后**那一条,
    #    所以判据换成"只有拼接才能产生"的特征:两个 `算力` 出现在同一条里。
    joined = [text for text in collected
              if text.count("算力") >= 2 and "需要" in text]
    assert joined, "JoinedStr(f-string)分支没取到东西 —— 而被改的那句正是 f-string"

