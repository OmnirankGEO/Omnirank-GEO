"""DeepSeek **官方线**模型名的唯一来源(WO_206)。

Owner 09-13:「我们系统需要干活的模型默认是官方版本的 deepseek-flash」。
DeepSeek 官方 2026-09 的口径:模型名请用 `deepseek-flash`;旧名
`deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 仍可调用但模型已下线,
由 V4.1-Flash 提供服务、按 Flash 价计费;`deepseek-v4-pro` 继续提供。

🔴 **只管官方线**(`api.deepseek.com` 与官方 key 池)。
   百炼 / DashScope 上的 `deepseek-v4-flash` 是**另一家的模型 ID** ——
   DeepSeek 改名不改它。把两条线一起改,百炼会收到一个它不认识的名字:
   运行时才报"模型不存在",或者更糟,静默落到别的档。
   全仓哪一行属于哪条线,见签入表
   `docs/AI-CONTEXT/DEEPSEEK_OFFICIAL_EMIT_SITES_2026-09-14.md`(逐行人读)。

🔴 一个名字扮**四个**角色,改「发」不改其余三个会静默降级:
     发 —— 请求体里的 model 字段(本模块的常量);
     认 —— 从 settings.json / 环境变量 / 库里读到的旧名(`normalize_deepseek_model`);
     比 —— 思考开关 `writing/llm_utils.py` 按前缀判断要不要发 thinking:disabled;
     分档 —— `deepseek_role_for_model` 按名字挑 key 池档位、`PRICING_TABLE` 按名计价。
   这四处任一漏改都**不报错**:开着思考慢 ~17% 贵 ~35%,计价落通用档约 2.5×
   并丢缓存折扣,key 池掉档换成另一批 key。
"""
from __future__ import annotations

from typing import Final

#: 官方线干活模型(Flash 档)。**唯一**默认。
DEEPSEEK_OFFICIAL_FLASH: Final[str] = "deepseek-flash"

#: 官方线质量档。09-14 之后官方继续提供,本单不改它的用法。
DEEPSEEK_OFFICIAL_PRO: Final[str] = "deepseek-v4-pro"

#: 官方线的历史名。**只用于"认"**(读到它们要归一),不再作为默认发出去。
#: 🔴 `deepseek-chat` 在官方定价页上已经不出现了(Review 09-13 夜实取),
#:    它现在指向哪一代、何时下线官方没说 —— 所以它只配当"认",不配当"发"。
#:
#: 🔴🔴 2026-09-14 **`deepseek-reasoner` 并入别名集**,推翻 c1a 的「reasoner 不归一」。
#:    c1a 当时的理由是「它是官方线上另一个模型(开思考那档),折进去会悄悄改掉
#:    调用方要的行为」—— 那个理由**建立在一个已经不成立的事实上**。
#:    Deploy 206-d2 实打(2026-09-14):官方 `/models` 只剩 `deepseek-flash` 与
#:    `deepseek-v4-pro`;`deepseek-reasoner` 仍返 200,但**回显 `deepseek-flash`**。
#:    也就是说"那一档"今天已经不存在了,四处发出点正在**静默降级**跑 flash。
#:    Owner 2026-09-14 拍板,原话:「全部改成 deepseek-flash」—— 明改,不升 v4-pro。
#:    所以现在归一不是"悄悄改行为",恰恰相反:是把已经发生的降级**写明**。
DEEPSEEK_OFFICIAL_ALIASES: Final[frozenset[str]] = frozenset({
    "deepseek-chat",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-reasoner",
})

#: 官方线允许**发出去**的名字全集。仓级锁按它判。
DEEPSEEK_OFFICIAL_EMITTABLE: Final[frozenset[str]] = frozenset({
    DEEPSEEK_OFFICIAL_FLASH, DEEPSEEK_OFFICIAL_PRO,
})


def normalize_deepseek_model(name: str | None) -> str | None:
    """官方线模型名归一:旧名 -> `deepseek-flash`;别的原样返回。

    用在**读配置**的地方(settings.json / 环境变量 / 库里存的值):
    存量配置里写的是旧名,归一之后下游的"比"和"分档"才认得出来。

    🔴 只归一**官方线**的别名。传进来一个百炼的名字会原样返回 ——
       本函数不知道调用方在哪条线上,所以它只做"这个名字是不是官方线的历史名"
       这一件事,判线的责任留在调用点(那里才知道 base_url)。
       换句话说:**在官方线的入口调它**,别在通用的模型解析里调。

    🔴🔴 `deepseek-reasoner` **归一**(2026-09-14 翻面,Owner 拍板)。
       这里原来写的是「不归一 —— 它是官方线上另一个模型(开思考那档),
       把它折进 flash 会悄悄改掉调用方要的行为」。那句话建立在一个
       **已经不成立的事实**上:Deploy 206-d2 当天实打,官方 /models 只剩
       deepseek-flash 与 deepseek-v4-pro,而 deepseek-reasoner 仍返 200
       却**回显 deepseek-flash** —— 那一档已经没有了。
       所以归一不是"悄悄改行为",是把**已经发生的静默降级**写明。
       🔴 注释与代码打架时,代码是对的那个;这段字是 09-14 补的,别再让它落后。
    """
    if name is None:
        return None
    raw = str(name).strip()
    if not raw:
        return raw
    return DEEPSEEK_OFFICIAL_FLASH if raw in DEEPSEEK_OFFICIAL_ALIASES else raw


def is_official_alias(name: str | None) -> bool:
    """这个名字是不是官方线的历史名(只用于判断,不改写)。"""
    return str(name or "").strip() in DEEPSEEK_OFFICIAL_ALIASES


class OfficialModelEchoMismatch(RuntimeError):
    """官方线回显的模型名与请求名不符。

    🔴 为什么这要单独成一类错,而不是记个日志:
       供应商静默把旧名映射到新模型时,**HTTP 200、内容正常、按新价计费**,
       账单不异常 —— 上一次两个多月无人发现(本仓
       `a-200-can-hide-a-silently-substituted-model`)。
       对**监测**来说更重:模型身份就是被测量本身。把 B 的回答记成 A 的,
       整列数据都不可解读,而没有任何东西会报错。
    """


def assert_official_echo(requested: str | None, echoed: str | None) -> None:
    """官方线**唯一**的回显判别点:回显必须等于请求名。

    🔴 与 `_assert_official_deepseek_shape` 的分工:
       那个验的是**协议形状**(Anthropic Messages vs 百炼的 `{"output": …}`),
       回答的是「打没打到官方通道」;它分不出**是哪个模型答的** ——
       同一条通道上 flash / v4-pro / 任何替换品的形状完全一样。
       两件事都要验,缺一个就留一个洞。

    🔴 `echoed` 为空**不当通过**:官方响应带 `model` 字段,没有它说明
       这根本不是我们以为的那条响应 —— 「字段缺失」比「值不同」更可疑,
       而宽容处理正是「一个会返 200 的错答案」得以长期存活的方式。
    """
    want = str(requested or "").strip()
    got = str(echoed or "").strip()
    if not want:
        raise OfficialModelEchoMismatch("请求名为空,无从比对")
    if not got:
        raise OfficialModelEchoMismatch(
            "响应里没有 model 字段(请求 %s)—— 官方响应应当回显模型名" % want)
    if got != want:
        raise OfficialModelEchoMismatch(
            "回显模型 %s != 请求 %s —— 供应商可能静默换了模型" % (got, want))
