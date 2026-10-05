"""推荐码/邀请码的归一化 —— 单点定义,读写两侧共用。

🔴 为什么要有这个(2026-08-05 生产事故):
   服务商「贵州AI数字技能就业实训基地」(#133)的码是 `OR-HTLO7LVQ`,
   第 7 位是**字母 O**。对方转录时输成了 `OR-HTL07LVQ`(数字 0),
   注册页直接回「推荐码无效或已失效」。

   实测(生产 2026-08-05 18:5x):
     OR-HTLO7LVQ → valid:true
     OR-HTL07LVQ → valid:false      ← 用户输的
     or-htlo7lvq → valid:false      ← **连大小写都不容错**

   而全站 49 个推荐码 **100% 含 [O0Il1] 这类易混字符**。
   也就是说这不是一个人的事:任何人手抄/口述/截图转录都可能挂,
   而挂掉的位置正是**注册漏斗的最后一步**。

🔴 为什么敢折叠(动手前先证的,不是想当然):
   折叠会把两个不同的码变成同一个 → 那就会把 A 的推荐算到 B 头上,
   是**资金归属错误**,比现在更糟。所以上线前先在生产库跑过:
     · referral_codes 49 个码 → 折叠后仍是 49 个唯一值(零碰撞)
     · invite_codes 0 行
     · 两表合起来也零碰撞
     · 反向对照:故意构造 OR-HTLO7LVQ / OR-HTL07LVQ 一对,查询把它们抓出来了
       → 说明那条「零碰撞」不是查询写废了

🔴 折叠方向选 字母→数字(O→0, I→1, L→1),不是反过来:
   因为码里前缀是 `OR-`,含字母 O。两边都折叠成 `0R-` 是一致的,不影响匹配;
   而若把数字折成字母,`0`→`O` 同样一致 —— 方向本身无所谓,
   **要紧的是读写两侧用同一个函数**,所以这里只允许有这一份实现。

⚠️ 本模块只影响**比对**,不改任何已存的码:库里存的还是原码,
   用户看到的、分享出去的也还是原码。
"""
from __future__ import annotations

# 易混字符折叠表:字母 → 形近数字
#   O/o → 0    I/i → 1    L/l → 1
# 🔴 不要往这里加 S↔5 / B↔8 / Z↔2:折叠得越多,碰撞概率越高,
#    而碰撞的代价是把佣金算到别人头上。加之前必须重跑上面那条零碰撞核验。
_FOLD_FROM = "OoIiLl"
_FOLD_TO = "001111"
_FOLD_MAP = str.maketrans(_FOLD_FROM, _FOLD_TO)

# 供 SQL 侧使用,保证两边**同一张折叠表**(不写两份,不会各自漂移)
SQL_FOLD_FROM = _FOLD_FROM
SQL_FOLD_TO = _FOLD_TO

# SQL 片段:对某一列做与 normalize_code() 等价的折叠。
#   用法:cursor.execute(f"... WHERE {fold_sql('code')} = {fold_sql('%s')}", (code,))
def fold_sql(expr: str) -> str:
    """返回把 `expr` 折叠后的 SQL 表达式(与 normalize_code 等价)。"""
    return f"upper(translate({expr}, '{SQL_FOLD_FROM}', '{SQL_FOLD_TO}'))"


def normalize_code(code: str | None) -> str:
    """把用户输入的码归一化成可比对的形式。

    做三件事,都只为「人手抄错」这一个场景:
      1. 去首尾空白 + 去掉中间的空格/全角空格(复制粘贴常带)
      2. 转大写(实测小写现在直接判无效)
      3. 折叠易混字符 O→0 / I→1 / L→1

    🔴 **不去掉连字符** —— `OR-` 前缀是码的一部分,去掉会让
       `OR-ABC` 和 `ORABC` 视为同一个,那是放宽而不是纠错。
    """
    if not code:
        return ""
    s = str(code).strip()
    # 全角空格 + 普通空格 + 不间断空格
    for ch in (" ", "　", " ", "\t"):
        s = s.replace(ch, "")
    return s.upper().translate(_FOLD_MAP)


# 生成新码时应当避开的字符(治本:以后不再产生会被看错的码)
#   0/O 1/I/L 一律不用;2/Z、5/S、8/B 保留(折叠表里没有它们,不影响比对)
SAFE_CODE_ALPHABET = "23456789ACDEFGHJKMNPQRTUVWXY"
