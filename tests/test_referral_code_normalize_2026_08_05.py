"""锁:推荐码比对必须容错(大小写 + 易混字符),但**不许放宽到会撞码**。

生产事故(2026-08-05):
  服务商 #133 的码是 `OR-HTLO7LVQ`(第 7 位=字母 O),对方转录成
  `OR-HTL07LVQ`(数字 0),注册页当场「推荐码无效或已失效」。
  实测:字母O版 valid:true / 数字0版 valid:false / 小写版也 valid:false。
  而全站 49 个码 **100% 含 [O0Il1]** —— 这不是一个人的事,是注册漏斗最后一步的通杀。

🔴 这个锁的重点不只是"能匹配上",更是"**不许匹配过头**":
  折叠得越多,两个不同的码撞成一个的概率越高,而撞了就是把 A 的推荐
  算到 B 头上 = 资金归属错误,比现在更糟。所以下面既有 must_hit 也有 must_not_hit。
"""
from __future__ import annotations

import io
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from services.referral_code_normalize import (  # noqa: E402
    SAFE_CODE_ALPHABET,
    fold_sql,
    normalize_code,
)


# ───────────────────────── 必须命中:真实事故那一对 ─────────────────────────

def test_A1_the_actual_incident_pair_now_matches__must_hit():
    """字母O版 与 数字0版 归一化后必须相等 —— 这就是 #133 那一单。"""
    assert normalize_code("OR-HTLO7LVQ") == normalize_code("OR-HTL07LVQ")


def test_A2_lowercase_matches__must_hit():
    """实测小写现在直接判无效,修完必须认。"""
    assert normalize_code("or-htlo7lvq") == normalize_code("OR-HTLO7LVQ")


def test_A3_whitespace_from_copy_paste__must_hit():
    """复制粘贴常带空格/全角空格。"""
    for raw in (" OR-HTLO7LVQ ", "OR-HTLO7LVQ\t", "OR- HTLO7LVQ", "OR-HTLO7LVQ"):
        assert normalize_code(raw) == normalize_code("OR-HTLO7LVQ"), raw


def test_A4_I_and_L_fold_to_1__must_hit():
    assert normalize_code("OR-I1L") == normalize_code("OR-111")


# ───────────────────── 必须不命中:不许放宽到会撞码 ─────────────────────

def test_B1_hyphen_is_not_stripped__must_not_hit():
    """🔴 连字符是码的一部分,去掉它就是放宽而不是纠错。"""
    assert normalize_code("OR-ABC") != normalize_code("ORABC")


def test_B2_unrelated_codes_stay_different__must_not_hit():
    """🔴 两个真实存在的、本来不同的码,折叠后必须仍然不同。"""
    real = [
        "OR-HTLO7LVQ", "OR-DWPTKBAY", "OR-QAKA5HUZ", "OR-J42MRNBG",
        "OR-3TUBU3TF", "OR-CXSB8STT", "OR-SE5KMXM2", "OR-AWAMESKT",
        "OR-3YMVZKKR",
    ]
    folded = [normalize_code(c) for c in real]
    assert len(set(folded)) == len(real), f"折叠后撞码:{folded}"


def test_B3_S5_B8_Z2_not_folded__must_not_hit():
    """🔴 没往折叠表里加 S↔5 / B↔8 / Z↔2 —— 加了碰撞概率上升。

    这条不是在锁"现在没加",是锁"加之前必须重跑零碰撞核验"这件事:
    谁哪天顺手加进去,这条会红,红了就得先去证碰撞。
    """
    assert normalize_code("OR-S") != normalize_code("OR-5")
    assert normalize_code("OR-B") != normalize_code("OR-8")
    assert normalize_code("OR-Z") != normalize_code("OR-2")


def test_B4_empty_stays_empty__must_not_hit():
    """空输入不许归一化成某个能匹配上的东西。"""
    for bad in (None, "", "   ", "　"):
        assert normalize_code(bad) == ""


# ───────────────────── SQL 侧与 Python 侧必须同一张折叠表 ─────────────────────

def test_C1_sql_and_python_use_same_fold_table__must_hit():
    """🔴 两侧折叠表若各自漂移,就会出现「校验放行、绑定不认」。"""
    expr = fold_sql("code")
    assert "translate(code," in expr
    assert "upper(" in expr
    from services.referral_code_normalize import SQL_FOLD_FROM, SQL_FOLD_TO

    assert SQL_FOLD_FROM in expr and SQL_FOLD_TO in expr
    assert len(SQL_FOLD_FROM) == len(SQL_FOLD_TO), "translate 两参数长度必须相等"


def test_C2_all_four_query_sites_use_fold_sql__must_hit():
    """🔴 四个查询点(注册/公开校验/绑定/管理员唯一性)一个都不能漏。

    漏掉管理员那处 = 能造出折叠后相同的两个码 = 归一化匹配把佣金算给错的人。
    """
    sites = {
        "api/auth_api.py": 1,
        "api/referral_api.py": 2,
        "api/admin_api.py": 1,
    }
    for rel, want in sites.items():
        src = io.open(REPO / rel, encoding="utf-8").read()
        got = src.count("fold_sql(")
        assert got >= want * 2, f"{rel} 里 fold_sql( 只出现 {got} 次,应 ≥{want*2}(每处两个)"


def test_C3_no_bare_exact_match_left__must_not_hit():
    """🔴 裸的精确匹配必须绝迹。"""
    bare = "referral_codes WHERE code = %s"
    for rel in ("api/auth_api.py", "api/referral_api.py", "api/admin_api.py"):
        src = io.open(REPO / rel, encoding="utf-8").read()
        assert bare not in src, f"{rel} 里还有裸精确匹配"


def test_C4_the_bare_pattern_would_be_caught__must_hit():
    """🔴 判别力自证:同一条判据打在修复前的原文上必须命中。"""
    old = '        cursor.execute("SELECT user_id FROM referral_codes WHERE code = %s", (code,))'
    assert "referral_codes WHERE code = %s" in old


# ───────────────────── 治本:新码字符集 ─────────────────────

def test_D1_safe_alphabet_excludes_confusables__must_not_hit():
    """新码字符集里不许再出现 0/O/1/I/L —— 以后不再产生会被看错的码。"""
    for ch in "0O1IL":
        assert ch not in SAFE_CODE_ALPHABET, f"{ch} 不该在安全字符集里"


def test_D2_safe_alphabet_is_usable__must_hit():
    """反向对照:字符集不能被删空了。"""
    assert len(SAFE_CODE_ALPHABET) >= 24
