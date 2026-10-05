"""WP4/WP6 · 入口闸的两张欠条 + 三页职责互斥的可执行验收。

两张欠条(WP2 起挂账,Review 要求终审前清):
  · **Demo 三零**:同路由同 DTO,写尝试零 DB 写 / 零外调 / 零 freeze
  · **503 E2E**:schema 未就绪 → 503 且零 claim / 零资金 / 零订单 / 零 provider
"""
from __future__ import annotations

import io
import pathlib
import re
import sys

import pytest

from services.geo_douyin.contract_route_guard import (
    ABILITY_PRODUCE, ABILITY_PUBLISH, GUARDED_MUTATION_ENDPOINTS,
    AbilityNotGranted, DemoReadOnly, SchemaNotReady,
    error_payload, guard_mutation,
)

REPO = pathlib.Path(__file__).resolve().parents[2]


class _Tripwire:
    """碰一下就炸的哨兵。比"断言没被调用"强:后者可能只是这次没走到那个分支。"""

    def __init__(self, name: str):
        self._name = name

    def __getattr__(self, attr):
        raise AssertionError(f"闸后仍触碰了 {self._name}.{attr} —— 三零断言破裂")


@pytest.fixture()
def tripwires(monkeypatch):
    """把资金 / 发布渠道 / 连接池三类副作用模块全换成会炸的哨兵。"""
    for module_name in ("middleware.billing",
                        "services.geo_douyin.publish_adapter",
                        "db.connection"):
        monkeypatch.setitem(sys.modules, module_name, _Tripwire(module_name))
    yield


# ============================================================
# 欠条 ① · 503 SCHEMA_NOT_READY 且零副作用
# ============================================================

def test_schema_not_ready_blocks_before_any_side_effect(tripwires):
    """schema 未就绪 → 抛 SchemaNotReady,且**在**任何资金/渠道/DB 动作之前。

    哨兵保证"之前"这件事是真的:闸后若有任何一行碰了那三个模块,当场 AssertionError。
    """
    with pytest.raises(SchemaNotReady) as excinfo:
        guard_mutation(schema_blockers_fn=lambda: ["missing_column:geo_douyin_posts.quote_id"],
                       is_demo=False, action="batches.create",
                       granted_abilities={ABILITY_PRODUCE}, required_ability=ABILITY_PRODUCE)
    assert excinfo.value.blockers


def test_schema_not_ready_payload_is_executable():
    payload = error_payload(SchemaNotReady(["missing_table:geo_douyin_post_revisions"]))
    assert payload["code"] == "SCHEMA_NOT_READY"
    assert payload["actions"] and payload["actions"][0]["type"] in {
        "retry", "nav", "contact", "dismiss", "api"}
    assert "kind" not in payload["actions"][0], "写了会被 builder 丢弃的 `kind`"
    # 影响面必须写清"没发生什么" —— 用户最怕的是"我是不是已经被扣钱了"
    for word in ("未创建", "未冻结", "未联系"):
        assert word in payload["impact"], payload["impact"]
    assert payload["rule_version"], "rule_version 为空 = 不可审计(规格 §12 禁 NULL 冒充版本)"


def test_schema_ready_lets_it_through(tripwires):
    """反向对照:就绪时必须放行,否则"未就绪拦住"可能只是"什么都拦"。"""
    guard_mutation(schema_blockers_fn=lambda: [], is_demo=False, action="batches.create",
                   granted_abilities={ABILITY_PRODUCE}, required_ability=ABILITY_PRODUCE)


# ============================================================
# 欠条 ② · Demo 三零
# ============================================================

def test_demo_mutation_is_blocked_with_zero_side_effects(tripwires):
    """演示模式下的写尝试:403,零 DB 写 / 零外调 / 零 freeze。"""
    with pytest.raises(DemoReadOnly):
        guard_mutation(schema_blockers_fn=lambda: [], is_demo=True, action="batches.create",
                       granted_abilities={ABILITY_PRODUCE}, required_ability=ABILITY_PRODUCE)


def test_demo_is_blocked_even_when_ability_is_granted(tripwires):
    """🔴 顺序判据:demo 必须排在 ability **之前**。

    演示用户可能**恰好**持有该能力(他是被授权看某品牌的真实用户),
    先判 ability 就会放行 → 真写库。这条锁死那个顺序。
    """
    with pytest.raises(DemoReadOnly):
        guard_mutation(schema_blockers_fn=lambda: [], is_demo=True, action="publish.submit",
                       granted_abilities={ABILITY_PUBLISH, ABILITY_PRODUCE},
                       required_ability=ABILITY_PUBLISH)


def test_schema_check_precedes_demo(tripwires):
    """schema 未就绪时,即使是 demo 也先报 schema —— 读权限本身也要查表。"""
    with pytest.raises(SchemaNotReady):
        guard_mutation(schema_blockers_fn=lambda: ["missing_table:x"], is_demo=True,
                       action="batches.create", granted_abilities=set(),
                       required_ability=ABILITY_PRODUCE)


def test_demo_payload_says_nothing_happened():
    payload = error_payload(DemoReadOnly("batches.create"))
    assert payload["code"] == "DEMO_READ_ONLY"
    assert "未产生任何数据变更" in payload["impact"]
    assert payload["actions"][0]["type"] == "dismiss"


# ============================================================
# 能力级授权(制作与发布分开)
# ============================================================

def test_default_sales_gets_handoff_not_execution(tripwires):
    """默认销售没有这两项能力 → 「交给交付同事 / 申请权限」,不是硬邦邦的 403。"""
    with pytest.raises(AbilityNotGranted) as excinfo:
        guard_mutation(schema_blockers_fn=lambda: [], is_demo=False, action="batches.create",
                       granted_abilities={"quote.create", "diagnosis.run"},
                       required_ability=ABILITY_PRODUCE)
    payload = error_payload(excinfo.value)
    labels = [a["label"] for a in payload["actions"]]
    assert "交给交付同事" in labels and "申请权限" in labels
    assert payload["actions"][0]["type"] in {"retry", "nav", "contact", "dismiss", "api"}


def test_produce_ability_does_not_grant_publish(tripwires):
    """🔴 规格 §9:写作/制作与发布**分别**授权。有制作权不等于能发布。"""
    guard_mutation(schema_blockers_fn=lambda: [], is_demo=False, action="batches.create",
                   granted_abilities={ABILITY_PRODUCE}, required_ability=ABILITY_PRODUCE)
    with pytest.raises(AbilityNotGranted, match=ABILITY_PUBLISH):
        guard_mutation(schema_blockers_fn=lambda: [], is_demo=False, action="publish.submit",
                       granted_abilities={ABILITY_PRODUCE}, required_ability=ABILITY_PUBLISH)


def test_guarded_endpoint_list_is_not_empty():
    """分母先立住:清单为空的话"每个端点都过闸"是恒真。"""
    assert len(GUARDED_MUTATION_ENDPOINTS) >= 6
    assert any("publish-batch" in p for p in GUARDED_MUTATION_ENDPOINTS)
    assert any("batches" in p for p in GUARDED_MUTATION_ENDPOINTS)


# ============================================================
# 三页职责互斥(01 §2 / §10「前端不可签收条件」)
# ============================================================

# [WO_271 · 2026-09-23] 页一再改锚:ImageNoteStudio.tsx 随 #204 a2 制作台退役删除(df9c142c5)。
#   现役「页一」是图文工作台的选题面板;算钱扫描改扫图文创作流**全部**文件(机械枚举,不手挑)。
PAGE_ONE = REPO / "frontend/src/pages/Writing/ImageNoteTopicPanel.tsx"
PAGE_TWO = REPO / "frontend/src/pages/Writing/DouyinPostDetail.tsx"
PUBLISH_PANEL = REPO / "frontend/src/pages/Publishing/ShortVideoPanel.tsx"


def _image_note_flow_files() -> list:
    """[WO_271] 图文创作流:pages/Writing 下 ImageNote* / imageNote* 机械枚举(不手挑)。"""
    writing = REPO / "frontend/src/pages/Writing"
    files = sorted(p for p in writing.iterdir()
                   if p.is_file() and p.suffix in (".ts", ".tsx")
                   and p.name.startswith(("ImageNote", "imageNote")))
    # 分母自证:枚举塌了 ⇒ 「一处都没有」是空话
    assert PAGE_ONE in files and len(files) >= 5, f"图文创作流只枚举到 {[p.name for p in files]}"
    return files


def _text(path: pathlib.Path) -> str:
    return io.open(path, encoding="utf-8", errors="ignore").read()


def test_frontend_files_exist():
    """分母:文件都在,后面的断言才有对象。"""
    for path in (PAGE_ONE, PAGE_TWO, PUBLISH_PANEL):
        assert path.is_file(), f"{path} 不存在 —— 后续前端判据全是恒真"


_RMB_PATTERN = re.compile(r"/\s*130\b|¥|人民币|yuanToPoints|pointsToYuan")


def _strip_ts_comments(text: str) -> str:
    """剥掉 `//` 行注释与 `/* */` 块注释再判。

    🔴 第一版没剥,当场被 `DouyinImagePost.tsx:346` 的一行**注释**顶红:
       `//    130 算力买到的东西不该被一次组件卸载吞掉` ——
       `//` 的第二个斜杠 + 空格 + `130` 正好命中 `/\\s*130`。
       那是一句解释性注释,不是前端算钱。
    🔴 这是本轮第**三**次踩「判据打在注释上」(前两次:SQL 形态锁、flag 扫描撞 RFC 文档)。
       教训不是"下次小心",是**扫描类判据默认先剥注释/文档**,把小心变成默认行为。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def test_no_rmb_or_130_conversion_in_image_note_pages():
    """🔴 01 §10 不可签收条件:页面内不许做 `/130`、人民币或进货价换算。

    这条**现在就能打**,不依赖三页重做 —— 而且它是资金红线(前端不算钱)。
    """
    offenders: dict[str, list[str]] = {}
    for path in (*_image_note_flow_files(), PAGE_TWO):   # [WO_271] 页一改扫图文创作流全部文件
        hits = [m.group(0) for m in _RMB_PATTERN.finditer(_strip_ts_comments(_text(path)))]
        if hits:
            offenders[path.name] = sorted(set(hits))
    assert not offenders, f"图文创作页在前端算钱(08_billing §3.3 前端不算钱):{offenders}"


def test_rmb_scan_criterion_actually_fires():
    """反向对照:扫描对真实违规**代码**必须命中,否则"没找到"只是正则抓不到。"""
    for sample in ("const p = points / 130;", "显示 ¥199", "yuanToPoints(x)", "人民币价格"):
        assert _RMB_PATTERN.search(_strip_ts_comments(sample)), sample
    assert not _RMB_PATTERN.search(_strip_ts_comments("const total = final_price_points;"))


def test_comment_stripping_does_not_hide_real_code():
    """🔴 剥注释这一步**自己**要有判别力对照:

    ① 注释里的 130 不该命中(否则判据对着解释文字恒红);
    ② **同一行**里注释之后有代码的情况不存在于 JS(// 之后到行尾都是注释),
       但**代码在前、注释在后**时,代码部分必须仍被扫到 —— 这条防止剥过头。
    """
    assert not _RMB_PATTERN.search(_strip_ts_comments("//    130 算力买到的东西"))
    assert _RMB_PATTERN.search(_strip_ts_comments("const p = x / 130; // 换算成人民币")), \
        "剥过头了 —— 注释前面的真代码也被吃掉"
    assert not _RMB_PATTERN.search(_strip_ts_comments("/* 每篇 130 算力 */"))


# ============================================================
# 规格与既有 Owner 裁决的冲突 —— 标记为**待裁**,不擅自改
# ============================================================

def test_viewport_user_scalable_conflict_is_flagged_not_silently_changed():
    """🔴 这条**不是**验收,是一条交接锁。

    规格 01 §8 要求「删除 `user-scalable=no`,浏览器 200% 缩放后仍可完整操作」。
    但 `frontend/index.html` 里那行是 **CTO-15.23 2026-05-21 v11** 的根治方案,
    注释逐字记着:Owner 11 次反馈 · v1-v10 都治标 · 真根因是 pinch zoom 后
    viewport 与 SidebarInset DOM 错位 · 明确写了「牺牲无障碍换 BUG-free」。

    这属于 **Owner 已拍板的客户可见行为**,不是我能顺手反转的技术细节
    (记忆纪律:业务语义/客户可见行为的决定权归 Owner)。
    因此本包**不动它**,把冲突显式挂在这里等裁定。

    技术补充(给裁定用):`user-scalable=no` 只影响**移动端 pinch zoom**,
    不影响桌面浏览器的 Ctrl+/- 页面缩放 —— 规格那条验收标准("200% 缩放")
    在桌面端**不受**该 meta 影响。真正受损的是移动端捏合放大。
    """
    html = _text(REPO / "frontend/index.html")
    assert "user-scalable=no" in html, (
        "有人删掉了 user-scalable=no —— 那是 CTO-15.23 v11 的 Owner 级根治方案,"
        "反转它需要 Owner 裁定;若已获裁定,请同步删掉这条交接锁"
    )
    assert "CTO-15.23" in html, "v11 决策的出处注释没了,冲突就无从追溯"
