"""[价格锁承诺脱钩修 2026-08-05 · WO_PRICE_LOCK_PROMISE] 报价单只承诺【真锁住】的价。

## 为什么要有这个模块

报价渲染层原来无条件写 `price_locked_until = 今天 + 7 天`,和「本次到底写没写共享缓存」
毫无关系:

    cache_expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")   # 无条件

而上游 `tools/batch_pricing._should_write_shared_cache` 会因为**正当理由**跳过写缓存
(P0-D 信任快照 per-brand 价 / 自设单篇成本 / 下级进货倍率 / C 端 skip_markup),
`_cacheable_rows` 还会再逐词剥掉爆价放飞词。两者叠加的结果是:
**上游因为正当理由跳过了锁,下游照样按锁在承诺。**

实证(2026-08-05):08-01~08-05 生成 6 单共 114 个计价词,同期 `keyword_price_cache`
写入 0 行,而报价单里 71/71 个词都带 `price_locked_until`。客户拿到「价格锁定至 X」,
三天后再算是另一个数 —— 撞「价格稳定感 ≥ 价格精确度」那条元指令。

## 铁律(改这个文件前先读)

1. **锁期日期一律来自 DB 的 `expires_at`**(写入函数的返回值 / 命中缓存行的列),
   渲染层**不许**自己 `now() + 7` —— 那正是本 bug 的形状。
2. **没锁 → 不给日期**,给一句人话;不给「缓存未命中」这种工程话
   (`feedback_hint_must_help_or_hide`)。
3. **粒度按词,不按单**:同一单里部分词写了缓存、部分被爆价护栏剥掉是正常情况,
   按单一刀切会制造新的不准。
4. 本模块**不判定**该不该写缓存,只把定价引擎【已有的结果】搬运出来。
   防污染闸(`_should_write_shared_cache`)是防跨客户价格污染的,必须留着。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

# 报价引擎挂在 quote_data 上的逐词锁期字典的键名。
# {keyword: "YYYY-MM-DD"} —— 只收录**本次真锁住**的词;没锁的词【不出现在这个 map 里】。
LOCK_MAP_KEY = "price_lock"

# 没锁住时给用户看的话。用户面话术:告诉他这个数字不能当一周的承诺发出去,
# 不提"缓存/命中/写入"这类工程词。
NO_LOCK_NOTE = "本次为即时计算价,下次查询可能变动"


def to_lock_date(expires_at: Any) -> str | None:
    """把 DB 的 `expires_at` 归一成 `YYYY-MM-DD`;取不到 / 已过期 → None(宁可不承诺)。

    入参三种真实形态都要吃:
      · psycopg2 读 TIMESTAMP 列 → `datetime`
      · save_* 写入时构造的 → ISO 字符串 `"2026-08-12T13:45:00.123456"`
      · 理论上的 `date`

    None 的含义是「不承诺锁期」,不是「出错了」—— 调用方据此**不显示**锁期。
    """
    if expires_at is None:
        return None

    parsed: date | None = None
    if isinstance(expires_at, datetime):
        parsed = expires_at.date()
    elif isinstance(expires_at, date):
        parsed = expires_at
    elif isinstance(expires_at, str):
        raw = expires_at.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            # 退一步:只取日期头 10 位(容忍 "2026-08-12 13:45:00" 这类空格分隔)
            try:
                parsed = date.fromisoformat(raw[:10])
            except ValueError:
                return None
    else:
        return None

    # fail-closed:算出来是过去的日期 = 这个"锁"已经不成立了,不许当承诺发出去。
    # (正常读路径带 `expires_at > CURRENT_TIMESTAMP` 过滤,走不到这里;
    #  留着是防将来有人绕开过滤直接塞行。)
    if parsed < date.today():
        return None
    return parsed.strftime("%Y-%m-%d")


def record_locked(lock_map: dict, keywords: Iterable[Any], expires_at: Any) -> int:
    """把【确实落进缓存 / 确实命中未过期缓存行】的词记上锁期。返回记上的词数。

    `expires_at` 解析不出来 → 一个都不记(不编日期)。
    调用点必须在「写入真的成功返回」/「读到真的缓存行」之后,不能提前乐观登记。
    """
    lock_until = to_lock_date(expires_at)
    if not lock_until:
        return 0
    n = 0
    for kw in keywords or ():
        if isinstance(kw, dict):
            kw = kw.get("keyword")
        if kw:
            lock_map[kw] = lock_until
            n += 1
    return n


def lock_fields(lock_map: dict | None, keyword: Any) -> dict:
    """报价渲染层的唯一出口:返回要合进【词级 DTO】的字段。

    真锁了 → `{"price_locked_until": "YYYY-MM-DD", "price_lock_note": None}`
    没锁   → `{"price_locked_until": None, "price_lock_note": NO_LOCK_NOTE}`

    两个键**恒定存在**(响应字段形状统一),且 `price_locked_until` 显式给 None 而不是
    省略键 —— 省略键会让下游 `kw.get("price_locked_until", now()+7)` 这类兜底把假日期
    复活(原社媒 agent 就是这么写的,该文件已随开源 E3 删除),整条修白做。
    """
    until = (lock_map or {}).get(keyword)
    if until:
        return {"price_locked_until": until, "price_lock_note": None}
    return {"price_locked_until": None, "price_lock_note": NO_LOCK_NOTE}
