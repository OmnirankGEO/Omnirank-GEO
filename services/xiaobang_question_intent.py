"""小榜「**问句**意图分层」—— 决定确定性答案与页面上下文谁说了算。

⚠️ 名字里的 intent 跟 `services/xiaobang_intent.py` **不是一回事**,别混:
   · `xiaobang_intent.py` = 五阶段**操作意图**状态机(prepare/confirm/execute,落库);
   · 本模块 = 一句话**问的是什么**的分层,纯函数、零副作用、不落库。
   两者唯一的关系是:本模块判「这句话该由确定性答案回答」时,
   确定性答案里可能带一个操作动作 —— 仅此而已。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 它修的是什么(工单 §4 P1-1)

改之前:``_should_short_circuit_canned`` 的规则是
    「**只要带了 current_page 或截图,就不许走 preset/canned**」。
而抽屉**每一次请求都带 current_page**(`useXiaobangChat` 恒传)。
⇒ 真实抽屉里 preset/canned **基本等于被全局关掉**:

  · 「你除了帮助中心还能做什么」命不中 `what_can_you_do`;
  · 同一句话在 API 直调和真实抽屉里得到**两种**结果 —— 而判据一直打的是直调那条。

这是把「页面提示」当成了「用户意图」。

## 改之后的规则(一句话)

**页面上下文只在用户真的在指页面时才压过确定性答案。**

判据不是「有没有 current_page」,而是「这句话里有没有**指示代词**指向当前页面」:
  · 「这个页面怎么用」「这页的按钮点不了」「当前页面这块是什么」→ 页面赢,走 RAG;
  · 「你能做什么」「怎么收费」「员工席位怎么用」→ 自足问句,确定性答案赢。

🔴 为什么判「指示代词」而不是判「问题里有没有页面名」:
   后者是**词表**,补一个漏三个;前者是**结构** —— 用户指当前页时必然用指示语,
   这是中文里绕不过去的形态。(同 `feedback_credibility_over_compliance_theater_in_geo`
   里那条「判据打结构不打词表」。)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import re

#: 指向「当前所见之物」的指示语。命中 = 这句话离开当前页面就没法解释。
#:
#: 🔴 「这个/那个」后面必须跟一个**可见物名词或问法**才算指页面 ——
#:    光一个「这个」也可能是承接上一轮对话(「这个怎么用」指的是上一轮说的功能),
#:    那种情况有 recent_turns 兜着,不该把它推给页面卡。
_PAGE_DEICTIC = re.compile(
    r"(这里|这块|此处)"
    r"|(这个|这|该|当前|本)\s*(页面?|界面|屏|画面|表单|弹窗|卡片|栏|区域)"
    r"|(这个|那个|这)\s*(按钮|输入框|选项|开关|下拉|标签页|图标|链接|数字|字段|报错|提示)"
    r"|(页面|界面)\s*(上|里|中|里面)\s*(的)?"
    r"|(截图|图|图片)\s*(里|中|上)"
    # 🔴 指示代词 + 操作动词:「这个怎么填」「这个该怎么用」「那个怎么点」
    #    中间允许 0-2 个字(「该」「要」「应」),但**必须**以指示代词开头 ——
    #    这样「应该怎么用」「员工席位怎么用」不会被误判成指页面。
    r"|(这个|那个|这)[^,，。;；!！?？]{0,2}(怎么|如何)(填|点|选|改|开|关|用|设置|操作|弄)"
    # 「这是什么/这是啥」—— 配合截图时几乎必然指屏幕上的东西
    r"|这是(什么|啥)"
)

#: 明确到不需要页面就能答的问句形态(自足意图)。
#: 这些即使带着 current_page 也必须让确定性答案赢。
_SELF_CONTAINED = re.compile(
    # 🔴 中间那段刻意放宽到 .{0,16}:真实用户会写
    #    「你**除了引导我去帮助中心**还能做什么」——第一版卡在 .{0,6},
    #    正好把工单 S03 那句原话漏掉(判据当场抓到)。
    r"(你|小榜).{0,16}?(能|会|可以|还能|除了).{0,16}?(做什么|干嘛|干什么|做啥|干啥)"
    r"|(还能|除了).{0,16}?(做什么|干嘛|干什么|做啥|干啥)"
    r"|(能力|功能)(有哪些|是什么|清单)"
    r"|(怎么|如何|多少).{0,4}(收费|计费|扣费|花钱|算力)"
    r"|(你是谁|你叫|谁开发|什么模型|真人吗|机器人)"
)


def mentions_current_page(message: str) -> bool:
    """这句话是不是在**指当前页面上看得见的东西**。"""
    return bool(_PAGE_DEICTIC.search(str(message or "")))


def is_self_contained_question(message: str) -> bool:
    """这句话是不是**离开页面也成立**的自足问句。"""
    return bool(_SELF_CONTAINED.search(str(message or "")))


def deterministic_answer_allowed(
    message: str,
    *,
    current_page: str = "",
    attachment_text: str = "",
) -> bool:
    """确定性答案(preset / canned FAQ)这一轮是否有权短路返回。

    规则(按优先级):
      1. **自足问句** → 永远允许(即使带页面、带截图)。
         这一条直接对着工单 S03「你除了帮助中心还能做什么」。
      2. 这句话在**指当前页面上的东西** → 不允许,交给页面卡 + RAG。
      3. 其余 → 允许。``current_page`` 不再是一道全局闸,它只在检索里做加权
         (`ROUTE_MATCH_MULT`,那部分没动)。

    ``attachment_text``(截图 OCR)同理:**它本身不再关闸**。
    截图 + 「这个按钮点不了」会被规则 2 拦下;截图 + 「你能做什么」不该被拦。
    """
    text = str(message or "")
    if is_self_contained_question(text):
        return True
    if mentions_current_page(text):
        return False
    return True


#: 对比/排除语气标记。出现在命中短语**之前**的小窗口里,说明用户是在
#: 「排除掉它」而不是「要它」。
#:
#: 🔴 这条修的是截图第 3 幕:「你**除了**引导我去帮助中心**还能**做什么」——
#:    `OperationRegistry.match` 是裸子串匹配,看到「帮助中心」就命中 help_center,
#:    于是用户明确说「除了帮助中心」,系统还是把他送回帮助中心。
#:    判的是**结构**(排除语气 + 命中短语的相对位置),不是词表。
_CONTRAST_MARKERS = ("除了", "除去", "除开", "不要", "别再", "别老", "不想", "不用",
                     "以外", "之外", "而不是", "不是要")

#: 命中短语前多少个字符内出现排除语气才算「在排除它」。
CONTRAST_WINDOW = 12


def is_contrastive_exclusion(message: str, phrase: str) -> bool:
    """`phrase` 在这句话里是被**排除**的对象吗。"""
    text = str(message or "")
    target = str(phrase or "")
    if not text or not target:
        return False
    idx = text.find(target)
    if idx < 0:
        return False
    window = text[max(0, idx - CONTRAST_WINDOW):idx]
    return any(marker in window for marker in _CONTRAST_MARKERS)


def should_suppress_operation_shortcut(message: str, phrases) -> bool:
    """这一轮该不该**放弃**「确定性操作直答」这条短路。

    两种情况放弃:
      1. 自足能力问句(「你还能做什么」)—— 用户要的是能力清单,
         不是被导航到某个页面;
      2. 命中的短语正被用户**排除**(「除了帮助中心还能…」)。

    ``phrases`` 传该 operation 的 display_name + synonyms(调用方从注册表拿,
    不在这里再抄一份词表)。
    """
    text = str(message or "")
    if is_self_contained_question(text):
        return True
    return any(is_contrastive_exclusion(text, p) for p in (phrases or ()))
