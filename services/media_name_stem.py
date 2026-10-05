"""[补充单 P0-1 2026-08-16] 媒体名词干判据 —— 「这个域名能不能唯一确定谁在发布」。

## 为什么换判据

原判据「同一注册域下 ≥5 个不同媒体名 ⇒ 平台」把**广告投放位当成了媒体**:

    china.com → 中华网快讯焦点图 / 中华网快讯首发 / 中华网快讯首页文字链 / 中华网快讯（视频）
    dzwww.com → 大众网健康 / 大众网健康首发 / 大众网健康首发(GEO) / 大众网济宁首发（医疗）

按数名字这两个都 ≥5;按「谁在发布」它们各只有一个主体。
Owner 2026-08-16 拍板:判据改成 **域名本身能不能唯一确定发布主体**。

## 指标怎么定的(两次失败尝试都留档,别再走一遍)

**失败尝试 ①「异名占比」**(阈值 30%)。四锚实跑:

    dzwww.com  异名 8/25 = 32%  → 判「平台」  ✗(Owner 定:非平台)
    ifeng.com  异名 4/35 = 11%  → 判「非平台」✗(Owner 已放 A 桶 = 平台)

两个错误要求的阈值方向**相反**(修 dzwww 要调高、修 ifeng 要调低)⇒ 占比在这两个域上
根本不可分,不是阈值没调好。真实差别在**异名背后站着几个主体**:

    dzwww.com 异名 = 海报新闻 / 海报新闻健康         → **1 个**主体(且与大众网同属大众报业)
    ifeng.com 异名 = PChome电脑之家 / 中国山东网 / 凤凰号 → **3 个**互不相干的主体

⇒ 判定量改为 `distinct_outsider_stems`:除主导主体外还站着几个独立发布者。
阈值由两轮实测定为 **3**(见 OUTSIDER_STEM_MIN_DISTINCT 注释)。

**失败尝试 ②「媒体名去噪」**(补充单 §1 要求的那一步,连同词表一起写了,又删了)。
补充单的判别力检验是「拆掉去噪 → china/dzwww 必须转回平台」。我拆了,**没转红**:

    全域 62 个可判定注册域,去噪开/关 → 判定翻转 **0** 个
    (三个分项 括号 / 版位词 / 地名后缀 单独关闭,也各是 0)
    零去噪下四锚**同样全对**

原因:版位噪声词都落在名字**尾部**(中华网快讯焦点图 → 前 3 字仍是「中华网」),
而判定读的是**前缀词干** —— 换成「数主体」之后,去噪要解决的问题已经被归并这一步吸收了。
且去噪有真实副作用(生产实测 43 个名字词干被改,含误剥:
`全球医疗网`→`全球网`、`万物推荐指南`→`万物指南`、`工业首发`→`工业`)。
**无判别力 + 有害 = 净负债,所以删掉,而不是留一个装饰**。

有判别力的反向对照是另一条:**把归并主体拆掉、退回旧判据「数不同媒体名」**——
`news.cn`(10 名) / `china.com`(42 名) / `dzwww.com`(25 名) 三个立刻从「非平台」翻回「平台」。
这条锁在 `tests/platform_domain_stem_2026_08_16/`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: 词干长度:中文品牌名普遍 3 字(新华网 / 中华网 / 大众网 / 凤凰网)。
#: 实测 2 会把「凤凰网」和「凤凰号」并成一个主体(ifeng 独立主体数 3→2,阈值窗口收窄);
#: 实测 4 会把「新华网上市公司」「新华网大首页」当成两个主体(news.cn / china.com / dzwww 全判错)。
STEM_LEN = 3
#: 🔴 判定阈值:除主导主体外还有几个**独立**发布者。>= 它 ⇒ 域名无法唯一确定发布主体 ⇒ 平台。
#: 取值由**两轮**实测定,不是拍的:
#:   第一轮 六个校准锚 → 窗口 [2,4](1 会把 china.com / dzwww.com 误判平台)
#:   第二轮 P0-4 抽样 30 个被判平台的域、逐条人肉判读 → 在窗口内择优:
#:     阈值 2:假阳性 3(itouchtv.cn / xnnews.com.cn / zhengguannews.cn)· 真平台漏判 0
#:     阈值 3:假阳性 1(zhengguannews.cn)                          · 真平台漏判 0  ← 取 3
#:     阈值 4:假阳性 0                                            · 真平台漏判 1(yoojia.com)
#:   三个假阳性同一形态:**3 字词干把同一地方媒体集团切开了**
#:   (广东台粤TV/触电新闻、咸宁名医网/新闻网/日报、正观新闻/郑州日报)。
#:   全库「恰好 2 个独立异名主体」这一档只有 2 个域,两个都是人肉确认的假阳性 ⇒ 2→3 不误伤真平台。
#: ⚠️ 这不是「为数字好看缩名单」(Owner 禁):itouchtv.cn 只有 8 条候选,74% 几乎不动;
#:   改的是一个**真实误判**。
OUTSIDER_STEM_MIN_DISTINCT = 3
#: 名字条数下限:样本太少词干统计没意义,一律判「样本不足」——**不判平台**,交人工。
MIN_NAMES_FOR_JUDGEMENT = 5


@dataclass
class StemVerdict:
    domain: str
    is_platform: bool
    reason: str
    name_count: int
    dominant_stem: str
    dominant_count: int
    outsider_count: int
    outsider_ratio: float
    #: 🔴 判定量:异名里有几个不同词干 = 除主导主体外还站着几个独立发布者。
    distinct_outsider_stems: int = 0
    outsider_stems: list[str] = field(default_factory=list)
    outsider_samples: list[str] = field(default_factory=list)
    name_samples: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "domain": self.domain, "is_platform": self.is_platform, "reason": self.reason,
            "name_count": self.name_count, "dominant_stem": self.dominant_stem,
            "dominant_count": self.dominant_count, "outsider_count": self.outsider_count,
            "outsider_ratio": round(self.outsider_ratio, 4),
            "distinct_outsider_stems": self.distinct_outsider_stems,
        }


def stem_of(name: str, stem_len: int = STEM_LEN) -> str:
    """媒体名 → 主体词干。原名直接取前缀,**不做去噪**(理由见模块 docstring 失败尝试 ②)。"""
    return str(name or "").strip()[:stem_len]


def classify_domain(
    domain: str,
    media_names: Iterable[str],
    *,
    stem_len: int = STEM_LEN,
    outsider_stem_min_distinct: int = OUTSIDER_STEM_MIN_DISTINCT,
    min_names: int = MIN_NAMES_FOR_JUDGEMENT,
    group_by_stem: bool = True,
) -> StemVerdict:
    """一个注册域是不是共享平台。

    `group_by_stem=False` 不是配置项,是**反向对照的拆解点**:关掉归并就退回旧判据
    「数不同媒体名 >= min_names」,`news.cn` / `china.com` / `dzwww.com` 必须转回「平台」。
    """
    names = [n for n in (str(x or "").strip() for x in media_names) if n]
    n = len(names)

    if not group_by_stem:
        # 旧判据留档:只数名字,不归并主体 —— 版位被当成媒体,正是本次要修的病。
        return StemVerdict(domain, n >= min_names,
                           f"[旧判据] {n} 个不同媒体名 {'>=' if n >= min_names else '<'} {min_names}",
                           n, "", 0, 0, 0.0, name_samples=sorted(names)[:8])

    if n < min_names:
        return StemVerdict(domain, False, f"样本不足({n} < {min_names})⇒ 不判平台,交人工",
                           n, "", 0, 0, 0.0, name_samples=sorted(names)[:8])

    stems: dict[str, int] = {}
    for name in names:
        stems[stem_of(name, stem_len)] = stems.get(stem_of(name, stem_len), 0) + 1
    dominant_stem, dominant_count = max(stems.items(), key=lambda kv: (kv[1], -len(kv[0])))
    outsiders = [x for x in names if stem_of(x, stem_len) != dominant_stem]
    outsider_stems = sorted({stem_of(x, stem_len) for x in outsiders})
    ratio = len(outsiders) / n

    is_platform = len(outsider_stems) >= outsider_stem_min_distinct
    if is_platform:
        shown = "、".join(outsider_stems[:4]) + ("…" if len(outsider_stems) > 4 else "")
        reason = (f"除主导主体「{dominant_stem}」外还有 {len(outsider_stems)} 个独立主体({shown})"
                  f" >= {outsider_stem_min_distinct} ⇒ 域名无法唯一确定发布主体")
    else:
        reason = (f"{dominant_count}/{n} 个名字共享词干「{dominant_stem}」,"
                  f"异名主体仅 {len(outsider_stems)} 个({'、'.join(outsider_stems) or '无'})"
                  f" < {outsider_stem_min_distinct} ⇒ 同一发布主体")
    return StemVerdict(domain, is_platform, reason, n, dominant_stem, dominant_count,
                       len(outsiders), ratio, len(outsider_stems), outsider_stems[:8],
                       outsider_samples=sorted(set(outsiders))[:8],
                       name_samples=sorted(names)[:8])


def classify_many(names_by_domain: dict[str, Sequence[str]], **kwargs: Any) -> dict[str, StemVerdict]:
    return {d: classify_domain(d, ns, **kwargs) for d, ns in names_by_domain.items()}
