"""关键词卫生闸的锁(WO_DELIVERY_FLYWHEEL_CLOSURE §2.4.2)。

起因:晨光富士付费监测配置里的「东菀载货电梯哪家好」/「东菀载人电梯哪家好」——
正确的字是**东莞**,却每天照常花 4 引擎的钱跑。

🔴 本文件的核心是 `test_every_confirmed_keyword_insert_site_is_gated`:
   2026-08-05「公司题漏进竞争层」那次的教训是 —— 4 个出口堵了 1 个,剩下 3 个降级出口
   全部绕过守卫,于是"修好了"其实等于没修。所以这里用 AST **枚举 server.py 里全部
   `INSERT INTO confirmed_keywords` 出口**,逐个要求其所在函数里有闸调用。
   新增一个没接闸的出口,这条锁必须变红。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════
# 1. 错字识别:必须命中 / 必须不命中 成对
# ══════════════════════════════════════════════════════════════
def test_detects_dongwan_typo_with_exact_suggestion():
    from services.keyword_hygiene import detect_place_typo

    hit = detect_place_typo("东菀载货电梯哪家好")
    assert hit is not None, "生产实测存在的错字词必须被认出来"
    assert hit["token"] == "东菀"
    assert hit["suggestion"] == "东莞", "必须给出确定的更正,否则没资格阻断"


def test_typo_is_found_at_any_offset_not_just_the_start():
    """🔴 候选切分必须是**滑窗**,不能是非重叠分块。

    开发时第一版用 `re.findall(r'[一-鿿]{2,3}')`,那是非重叠切分:
    「东菀载货电梯哪家好」被切成「东菀载」「货电梯」「哪家好」,「东菀」一次都不出现。
    而且这个坑很会骗人 —— 当错字恰好落在偶数偏移时,非重叠切分照样能切到,
    只用一条首位用例的锁**杀不掉这个变异**(实测 M10 一开始就存活了)。
    所以这里专门放一条**奇数偏移**的用例。
    """
    from services.keyword_hygiene import detect_place_typo

    hit = detect_place_typo("找东菀载货电梯哪家好")   # 「东菀」起始偏移 = 1(奇数)
    assert hit is not None and hit["suggestion"] == "东莞"


def test_correct_place_name_is_not_flagged():
    """反向对照:正确的词必须放行。缺了它,把 detect 写成恒真也能让上一条绿。"""
    from services.keyword_hygiene import detect_place_typo

    assert detect_place_typo("东莞载货电梯哪家好") is None
    assert detect_place_typo("深圳全屋定制哪家好") is None
    assert detect_place_typo("惠州载货电梯哪家好") is None


def test_real_business_words_are_not_false_flagged():
    """🔴 阻断闸的误报比漏报更糟 —— 这些都是一字之差但**必须放行**的真实业务词。

    开发时实测:只用"编辑距离 1"会把它们全判成错字并**阻断用户**:
      大型 vs 大鹏 / 长期 vs 长安 / 海外 vs 海口 / 中间 vs 中山 / 三年 vs 三亚
    所以判据加了第二个条件(变化的字必须形近/音近)。这条测试就是那道闸的守卫。
    """
    from services.keyword_hygiene import detect_place_typo

    for word in ("大型载货电梯哪家好", "长期维保服务哪家好", "海外案例参考",
                 "中间层设备选型", "三年质保的电梯品牌"):
        assert detect_place_typo(word) is None, f"「{word}」是正常业务词,绝不能被阻断"


def test_confusable_table_is_required_not_optional():
    """反向对照:把混淆表条件拿掉,「大型」就会被判成「大鹏」——证明这条件确实在起作用。"""
    from services.keyword_hygiene import _edit_distance_one, _confusable_substitution

    assert _edit_distance_one("大型", "大鹏") is True, "它们确实一字之差(前提成立)"
    assert _confusable_substitution("大型", "大鹏") is False, "但字不形近 → 必须判否"
    assert _confusable_substitution("东菀", "东莞") is True, "真错字必须判是"


def test_edit_distance_is_substitution_only():
    """不做插入/删除:否则「深圳」vs「深圳市」会互判为错字,制造噪声。"""
    from services.keyword_hygiene import _edit_distance_one

    assert _edit_distance_one("东菀", "东莞") is True
    assert _edit_distance_one("深圳", "深圳市") is False, "长度不同必须判否"
    assert _edit_distance_one("东莞", "东莞") is False, "完全相同不是错字"
    assert _edit_distance_one("广深", "东莞") is False, "两字都不同不是一字之差"


def test_brand_declared_city_is_whitelisted():
    """品牌自己声明的小地名不能被误判成错字。"""
    from services.keyword_hygiene import check_keywords

    # 「坪山」在表里;构造一个表外小地名,声明后必须放行
    brand = {"cities": "广东省深圳市观澜街道", "city_scope": "local"}
    clean = check_keywords(["观澜全屋定制哪家好"], brand=brand, with_history=False)
    assert not clean["blocking"], clean


# ══════════════════════════════════════════════════════════════
# 2. 三条规则的严格度必须分开(错字拦 / 其余只提示)
# ══════════════════════════════════════════════════════════════
def test_typo_blocks_but_too_few_only_advises():
    from services.keyword_hygiene import check_keywords

    typo = check_keywords(["东菀载货电梯哪家好"], with_history=False)
    assert typo["blocking"], "错字必须进 blocking"

    few = check_keywords(["东莞载货电梯哪家好"], existing_count=0, with_history=False)
    assert not few["blocking"], "词数不足绝不能阻断 —— 拦一个帮不上忙的提示就是无效警告"
    assert any(a["rule"] == "too_few_keywords" for a in few["advisory"])


def test_blocking_payload_carries_fixed_keyword():
    """阻断必须附带可以直接用的更正词,否则用户被拦住却不知道怎么办。"""
    from services.keyword_hygiene import check_keywords

    report = check_keywords(["东菀载人电梯哪家好"], with_history=False)
    issue = report["blocking"][0]
    assert issue["fixed_keyword"] == "东莞载人电梯哪家好"


def test_assert_keywords_clean_raises_400_only_for_blocking():
    from fastapi import HTTPException
    from services.keyword_hygiene import assert_keywords_clean

    with pytest.raises(HTTPException) as exc:
        assert_keywords_clean(["东菀载货电梯哪家好"])
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "KEYWORD_HYGIENE_BLOCKED"

    # 反向对照:干净的词必须不抛
    ok = assert_keywords_clean(["东莞载货电梯哪家好", "惠州载货电梯哪家好", "深圳载货电梯哪家好"])
    assert ok["checked"] == 3


# ══════════════════════════════════════════════════════════════
# 3. 出口枚举锁 —— 本文件最重要的一条
# ══════════════════════════════════════════════════════════════
# 🔴 本文件的锁**不剥注释**:它们全部打在 AST 节点上(字符串常量 / Call 节点),
#    注释本来就不进 AST。开发时先写了一个 tokenize 剥注释的 helper,结果它把 server.py
#    重建成了语法错误 —— 给判据加一道它根本不需要的工序,只会让判据自己坏掉。
def _functions_writing_confirmed_keywords(tree: ast.AST) -> dict[str, ast.AST]:
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if "INSERT INTO confirmed_keywords" in sub.value:
                    out[node.name] = node
                    break
    return out


def test_every_confirmed_keyword_insert_site_is_gated():
    """server.py 里每一个写 confirmed_keywords 的函数,都必须调过卫生闸。

    🔴 这条锁是**按出口枚举**而不是按数量断言:新加一个出口,它自动变红。
    """
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    writers = _functions_writing_confirmed_keywords(tree)
    assert writers, "没找到任何写入点 = 判据失效(锁自己坏了),不是'没有出口'"

    ungated = []
    for name, func in writers.items():
        gated = any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "_hygiene_gate_keywords"
            for sub in ast.walk(func)
        )
        if not gated:
            ungated.append(name)

    assert not ungated, (
        f"以下写 confirmed_keywords 的函数没过卫生闸,错字词可以从这里绕进付费配置: {ungated}"
    )


def test_gate_helper_itself_exists_and_is_fail_open():
    """闸自身异常绝不能打断主链(付费主流程 > 拼写检查)。"""
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "_hygiene_gate_keywords"),
        None,
    )
    assert helper is not None, "收口 helper 必须存在"
    handlers = [n for n in ast.walk(helper) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "helper 必须自带兜底,不能把闸的内部异常抛给付费主链"


def test_pipeline_writer_records_but_does_not_block():
    """诊断/选词流水线路径(db/diagnosis_db.save_confirmed_keywords)只留痕不阻断。

    这条锁固定的是一个**有意的边界**,不是遗漏:那条路径有 12 处调用方,在 DB 层抛
    400 会打断主收入链。边界写在交付说明里,锁在这里防止有人后来"顺手"改成阻断。
    """
    source = (ROOT / "db/diagnosis_db.py").read_text(encoding="utf-8")
    assert "from services.keyword_hygiene import check_keywords" in source, "必须留痕"
    assert "assert_keywords_clean" not in source, (
        "流水线路径不得改成阻断 —— 要改必须先评估 12 处调用方,并更新交付说明里的边界声明"
    )
