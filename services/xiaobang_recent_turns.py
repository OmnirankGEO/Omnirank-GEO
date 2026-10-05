"""小榜「有界最近对话」净化器 —— 唯一谓词。

工单 `WORKORDER_XIAOBANG_SOLUTION_FIRST_AI_2026-08-20` §4 P1-2 / §6 包 A③④⑤。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么单独一个模块,而不是写在 `api/xiaobang_api.py` 里:

  **同一个谓词写两处,必有一处没人验。** 浏览器那头也要挑「最近几轮」,
  如果两边各写一份「什么算合法历史」,两份会各自漂 —— 而判据只会打其中一份。
  这里是**服务端唯一权威**:浏览器提交的一切都当不可信输入,由本函数说了算;
  浏览器那头只做「少发点」的优化,不做「什么合法」的判断。

🔴 合同:**fail-open,永不 422**。
  历史脏了就丢那几条,不让整个问答请求挂掉 —— 问答链路不该因为
  localStorage 里躺着一条坏数据就对用户整条失败。
  (本仓吃过「response_model extra=forbid 让端点必 500」的亏三次;
   这里从一开始就不给自己留那个形态。)

🔴 会话记忆的真相口径(§6 包 A⑤):
  服务端**不持久化**任何对话。`session_id` 只是浏览器侧的会话相关 id,
  用来分 localStorage 的池子;它**不代表服务端记得你**。
  「记忆」完全来自本次请求里随身带的这几轮 —— 所以它有界、可审计、
  且用户清掉浏览器历史后服务端这边不残留。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import re
from typing import Any

# 最近 N 轮(工单建议 4-6)。一轮 = 一条消息,不是一问一答。
MAX_TURNS = 6
# 单条上限:超出截断,不丢整条(用户那句问题的开头通常就够消歧)
MAX_TURN_CHARS = 800
# 总字符上限:从**最近**往前收,保住离当前问题最近的那几轮
MAX_TOTAL_CHARS = 3000

ALLOWED_ROLES = ("user", "assistant")

# 🔴 §6 包 A④:旧截图的二进制/OCR 大块绝不能混进历史。
#    浏览器那头已经在 persist 时剥掉 previewUrl,但**不能靠上游守规矩** ——
#    这里再剥一次,并且判据直接打这里。
_DATA_URI = re.compile(r"data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+")
# 裸 base64 长块(>=256 连续 base64 字符且不含空白)—— 典型是被塞进正文的图片
_BARE_BLOB = re.compile(r"[A-Za-z0-9+/]{256,}={0,2}")

_REDACTED = "[图片]"


def _strip_binary(text: str) -> str:
    """剥掉 data: URI 与裸 base64 长块,保留其余正文。

    刻意**不丢整条**:那句话里除了图片还有用户的真问题,丢了就等于忘了上文。
    """
    text = _DATA_URI.sub(_REDACTED, text)
    text = _BARE_BLOB.sub(_REDACTED, text)
    return text


def sanitize_recent_turns(raw: Any) -> list[dict[str, str]]:
    """把浏览器提交的 `recent_turns` 净化成可以进 LLM 的有界历史。

    返回的每一项形如 ``{"role": "user"|"assistant", "content": str}``,
    按时间**正序**(最早的在前),可直接拼进 chat messages。

    丢弃规则(每一条都有对应判据 + 反向样本):
      · 不是 list → 返回 []
      · 条目不是 dict → 丢
      · role 不在 {user, assistant} → 丢(system/tool/developer 一律不认,
        否则前端能借历史往 system 位注入指令)
      · content 不是 str 或 strip 后为空 → 丢
      · content 超 MAX_TURN_CHARS → **截断**(不丢)
      · data: URI / 裸 base64 长块 → 就地替换成 "[图片]"(不丢整条)
      · 超 MAX_TURNS → 只留**最近** MAX_TURNS 条
      · 总长超 MAX_TOTAL_CHARS → 从最近往前收,收到装不下为止
    """
    if not isinstance(raw, list):
        return []

    cleaned: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ALLOWED_ROLES:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        content = _strip_binary(content).strip()
        if not content:
            continue
        cleaned.append({"role": role, "content": content[:MAX_TURN_CHARS]})

    # 只保留最近 MAX_TURNS 条
    cleaned = cleaned[-MAX_TURNS:]

    # 总长上限:**从最近往前**收 —— 离当前问题越近的越该保住
    kept: list[dict[str, str]] = []
    total = 0
    for item in reversed(cleaned):
        length = len(item["content"])
        if total + length > MAX_TOTAL_CHARS:
            break
        total += length
        kept.append(item)
    kept.reverse()
    return kept


def turns_as_chat_messages(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """净化后的历史 → OpenAI 兼容 chat messages 片段。

    这里刻意是一个**独立函数**而不是内联:判据要能单独驱动
    「净化后的东西真的以 user/assistant 身份进了 messages」,
    而不是只验「净化函数返回了什么」。
    """
    return [{"role": t["role"], "content": t["content"]} for t in turns]
