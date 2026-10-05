"""表面血缘目录(§5.2 / §3)。

把"平台名 / 真实产品表面 / 调用接口 / 模型版本"四轴拆开,消除 ai_tester 里
单一 "engine" 串糊四轴的反模式。每个 surface 的身份在此登记为 SSOT;实际每次调用
的 model/version 由 adapter 从真实响应或运行时 resolver 捕获写入 envelope。

Codex 补充裁定:
  #3 DeepSeek 官方 deepseek-v4-flash = deepseek_native_no_search;DashScope 仅
     deepseek_dashscope_search_legacy;秘塔只能标 deepseek_metaso_proxy。

  🔴 #3 已于 2026-07-27 被实测推翻(证据:docs/AI-CONTEXT/ENGINE_SEARCH_FIDELITY_EVIDENCE_2026-07-27.md):
     官方 `https://api.deepseek.com/anthropic/v1/messages` **接受** Anthropic 规范的
     `{"type":"web_search_20250305","name":"web_search"}` 工具声明,响应真实出现
     `server_tool_use` + `web_search_tool_result`,`usage.server_tool_use.web_search_requests` 有计数。
     故 deepseek_native_with_search 由 unavailable 转 active,并成为 deepseek 的默认表面。
     这不是"冒名",是原来那条"无可验证原生搜索"的判断已被真实响应否定。
  #4 元宝固定 HY3_API_KEY + https://tokenhub.tencentmaas.com/v1 + hy3 + yuanbao_hy3_tokenhub。

本文件零 DB / 零网络依赖(只读常量与纯函数),可安全 import。
"""

from __future__ import annotations
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from services.ai_surface_monitoring.contracts import SURFACE_KEYS


# 表面可用性:
#   active        = 已实现自动 adapter,可进采样
#   unavailable   = 结构保留但无可验证 API(如官方 DeepSeek 原生搜索);探针恒 unavailable,禁止路由
#   manual_only   = 仅人工采集研究表面,无自动 adapter
Availability = str  # Literal["active", "unavailable", "manual_only"]


@dataclass(frozen=True)
class SurfaceSpec:
    """一个表面的静态血缘登记。"""

    surface_key: str
    platform_key: str            # 产品/平台标识(doubao/qwen/deepseek/yuanbao/kimi)
    product_label: str           # 面向管理员的产品名(元宝/豆包/千问/DeepSeek/Kimi)
    provider_key: str            # 真实 API 供应商(volcengine/dashscope/deepseek/tencent_tokenhub/moonshot)
    default_model_key: str       # 缺省模型(实际调用可被运行时 resolver 覆盖)
    canonical_monitoring_platform: str  # 映射到 db.monitoring_db.PLATFORM_WEIGHTS 的 canonical 平台名
    availability: Availability = "active"
    # OpenAI 兼容表面的 base_url(仅新建的 hy3 / deepseek 原生用);百炼/火山走各自原生 SDK/HTTP。
    base_url: Optional[str] = None
    # API key 环境变量名(只从 env 读;禁止硬编码/入 SystemSettings)。
    env_key_var: Optional[str] = None
    # geo_research_config 里的模型名 key(env>DB>default);None 表示模型固定不可配置。
    model_config_key: Optional[str] = None
    # 缺省是否属于"新售默认全量矩阵"。Kimi=False(退出默认);历史订单权益由订单快照另行决定。
    default_enabled: bool = False
    # 历史只读平台(Kimi):不删历史、不进新默认与公共基线。
    legacy_read_only: bool = False
    # 该表面默认是否联网检索(provider 不披露则由 adapter 置 None)。
    default_search_enabled: Optional[bool] = None
    search_provider: Optional[str] = None


# ---------------------------------------------------------------------------
# 表面目录 SSOT
# ---------------------------------------------------------------------------

SURFACE_SPECS: Dict[str, SurfaceSpec] = {
    # ---- 新售默认四核矩阵 ----
    "doubao_ark_api_search": SurfaceSpec(
        surface_key="doubao_ark_api_search",
        platform_key="doubao",
        product_label="豆包",
        provider_key="volcengine",
        default_model_key="doubao-seed-2-0-lite-260215",
        canonical_monitoring_platform="doubao",
        env_key_var="VOLC_API_KEY",  # 实际链:VOLC_API_KEY > DOUBAO_SEED_API_KEY > DOUBAO_API_KEY
        model_config_key="model_doubao_app",
        default_enabled=True,
        default_search_enabled=True,
        search_provider="doubao-native-search",
    ),
    "qwen_dashscope_search": SurfaceSpec(
        surface_key="qwen_dashscope_search",
        platform_key="qwen",
        product_label="千问",
        provider_key="dashscope",
        default_model_key="qwen-plus-latest",
        canonical_monitoring_platform="dashscope",
        env_key_var="DASHSCOPE_API_KEY",
        model_config_key="model_qwen_default",
        default_enabled=True,
        default_search_enabled=True,
        search_provider="dashscope-search",
    ),
    # DeepSeek 官方原生(无搜索) —— Codex #3 要求新增,诚实标注
    "deepseek_native_no_search": SurfaceSpec(
        surface_key="deepseek_native_no_search",
        platform_key="deepseek",
        product_label="DeepSeek",
        provider_key="deepseek",  # 官方 api.deepseek.com
        #: 🔴 [WO_221-c1] 官方线(provider_key=deepseek · DEEPSEEK_API_KEY),本面 default_enabled=False。
        #:   官方页 `deepseek-v4-flash` 已退役。**两条端点行为不同**(2026-09-15 各打一发实测):
        #:     /v1/chat/completions   请求旧名 -> 200,回显 `deepseek-flash`(厂商归一)
        #:     /anthropic/v1/messages 请求旧名 -> 200,回显 `deepseek-v4-flash`(**原样**)
        #:   ⇒ 在 Anthropic 那条端点上回显锁是**同义反复**,抓不到「旧名被 flash 承接」;
        #:     那条线的防线是**发出前先归一**。
        default_model_key=DEEPSEEK_OFFICIAL_FLASH,
        canonical_monitoring_platform="deepseek",
        base_url="https://api.deepseek.com/v1",
        env_key_var="DEEPSEEK_API_KEY",
        model_config_key=None,  # 官方模型固定;不复用 dashscope 的 geo_research_config key
        # [2026-07-27] 默认表面已让位给 deepseek_native_with_search(官方原生搜索实测可用)。
        # 本表面保留:它是"官方模型但不开搜索"的诚实登记,对照实验与降级排查都要用到。
        default_enabled=False,
        default_search_enabled=False,   # 不声明工具 = 真的不搜(已实测:无 server_tool_use 块)
        search_provider=None,
    ),
    # [2026-07-27 实测转正] 元宝联网:TokenHub 自带能力,同端点同 key,只加 web_search_options。
    #   前置:TokenHub 控制台"联网搜索"已于 2026-07-27 开通(5 万次免费额度)。
    #
    # 🔴🔴 [2026-08-03 订正] 模型由 `hy3-preview` 改回 **`hy3`**。两个理由,缺一不可:
    #
    #   ① `hy3-preview` **2026-08-31 下线**(TokenHub 控制台模型列表标注),且其免费体验包
    #      已耗尽、未开后付费 → 生产实测 HTTP **402 / error.code=401008**
    #      ("服务免费体验额度已耗尽,且未开启后付费")。当天 24 次调用全 402,
    #      监测/诊断侧元宝样本恒为 0(`llm_call_results` 有史以来零成功)。
    #
    #   ② 上面原写着「实测 `hy3` 传了 web_search_options 也**静默不搜**」——
    #      **这条已被 2026-08-03 生产容器内实测推翻**。同一问题、同一 key、只换模型名:
    #        · `hy3` + web_search_options → HTTP 200,`search_results` **11 条真实 URL**
    #          (zhihu.com / mtjzjy.cn / toutiao.com),`usage.tool_usage={"web_search_call":3}`,
    #          `prompt_tokens` **16039**,答案自述"根据搜索结果"并带角标 [1];
    #        · 同模型不带 web_search_options 的成对对照 → `prompt_tokens` **35**、
    #          `search_results=None`、答案自述"目前是2024年"(模型知识截止)。
    #      三个独立信源(search_results 内容 / tool_usage 计数 / token 量差 458 倍)+ 行为差异
    #      共同证明 `hy3` **确实联网检索**。原结论在 07-27 当时可能成立(供应商此后放开),
    #      但它是有署名的判断,所以在这里**留痕订正而不是抹掉**。
    #
    #   🔴 改模型是**四处血缘同步**的事,漏一处就静默算错:本文件 + `db/monitoring_db.py`
    #      成本映射 + `tools/monitoring/batch_monitor.py` 表面映射 + `tools/llm_call_tracker.py`
    #      价目表(后者已含 `("tencent_tokenhub","hy3")` 同价条目,故本次不会重演 07-27 那次
    #      "价目表没有新模型名 → 落 DEFAULT_PRICING → 带缓存时虚高 38.6%" 的回归)。
    #      `hy3-preview` 的价目条目**保留不删** —— 历史数据仍要按当时模型算价。
    "yuanbao_hy3_tokenhub": SurfaceSpec(
        surface_key="yuanbao_hy3_tokenhub",
        platform_key="yuanbao",
        product_label="元宝",
        provider_key="tencent_tokenhub",
        default_model_key="hy3",
        canonical_monitoring_platform="yuanbao",
        base_url="https://tokenhub.tencentmaas.com/v1",
        env_key_var="HY3_API_KEY",         # Codex #4 固定
        model_config_key=None,             # 模型固定,不可配置改成别的模型
        default_enabled=True,
        default_search_enabled=True,       # 实测 2026-08-03:hy3 + web_search_options 真返 11 条 search_results
        search_provider="tencent_tokenhub",
    ),
    # ---- 历史/legacy 表面 ----
    # DashScope 托管的 deepseek-v4-flash(带 dashscope enable_search)= legacy,非官方原生
    "deepseek_dashscope_search_legacy": SurfaceSpec(
        surface_key="deepseek_dashscope_search_legacy",
        platform_key="deepseek",
        product_label="DeepSeek",
        provider_key="dashscope",  # 实际供应商是阿里百炼,不是 deepseek 官方
        default_model_key="deepseek-v4-flash",
        canonical_monitoring_platform="deepseek",
        env_key_var="DASHSCOPE_API_KEY",
        model_config_key="model_deepseek_via_dashscope",
        default_enabled=False,  # 非新售默认;保留作对照/兼容
        default_search_enabled=True,
        search_provider="dashscope-search",
    ),
    # [2026-07-27 实测转正] DeepSeek 官方原生搜索 —— 现为 deepseek 的默认表面。
    #   端点是 Anthropic 兼容的 /anthropic(不是 OpenAI 兼容的 /v1),协议差异见 ai_tester
    #   query_deepseek_official 的 docstring。检索由 DeepSeek 服务端执行,我们不自建检索层。
    "deepseek_native_with_search": SurfaceSpec(
        surface_key="deepseek_native_with_search",
        platform_key="deepseek",
        product_label="DeepSeek",
        provider_key="deepseek_official",
        #: 🔴 [WO_221-c1] 官方线(provider_key=deepseek_official · DEEPSEEK_API_KEY)· availability=active · default_enabled=True —— **真正在污染监测数据的就是这一面**,工单没点到。
        #:   官方页 `deepseek-v4-flash` 已退役。**两条端点行为不同**(2026-09-15 各打一发实测):
        #:     /v1/chat/completions   请求旧名 -> 200,回显 `deepseek-flash`(厂商归一)
        #:     /anthropic/v1/messages 请求旧名 -> 200,回显 `deepseek-v4-flash`(**原样**)
        #:   ⇒ 在 Anthropic 那条端点上回显锁是**同义反复**,抓不到「旧名被 flash 承接」;
        #:     那条线的防线是**发出前先归一**。
        default_model_key=DEEPSEEK_OFFICIAL_FLASH,
        canonical_monitoring_platform="deepseek",
        base_url="https://api.deepseek.com/anthropic",
        env_key_var="DEEPSEEK_API_KEY",
        availability="active",
        default_enabled=True,
        default_search_enabled=True,
        search_provider="deepseek_native",
    ),
    # ---- 结构保留但无自动可用性的表面 ----
    # 秘塔:DeepSeek 模型 + 外部检索代理(过渡态)。仅在配置了 METASO 时可用。
    "deepseek_metaso_proxy": SurfaceSpec(
        surface_key="deepseek_metaso_proxy",
        platform_key="deepseek",
        product_label="DeepSeek",
        provider_key="metaso",
        default_model_key="deepseek-v4-flash",
        canonical_monitoring_platform="deepseek",
        env_key_var="METASO_API_KEY",
        availability="unavailable",  # 未纳入本批 active adapter;探针据配置反映
        default_enabled=False,
        default_search_enabled=True,
        search_provider="metaso",
    ),
    # 腾讯 WSA:独立搜索能力,不能覆盖 Hy3 的 answer/citation/search 状态
    "tencent_wsa_search": SurfaceSpec(
        surface_key="tencent_wsa_search",
        platform_key="yuanbao",
        product_label="元宝",
        provider_key="tencent_wsa",
        default_model_key="wsa",
        canonical_monitoring_platform="yuanbao",
        availability="unavailable",  # 单独启用才 active;本批不默认启用
        default_enabled=False,
        default_search_enabled=True,
        search_provider="tencent_wsa",
    ),
    # 未来产品研究表面 / 人工采集
    "yuanbao_app_verified": SurfaceSpec(
        surface_key="yuanbao_app_verified",
        platform_key="yuanbao",
        product_label="元宝",
        provider_key="yuanbao_app",
        default_model_key="yuanbao-app",
        canonical_monitoring_platform="yuanbao",
        availability="manual_only",
        default_enabled=False,
    ),
    "manual_app_capture": SurfaceSpec(
        surface_key="manual_app_capture",
        platform_key="manual",
        product_label="人工采集",
        provider_key="manual",
        default_model_key="manual",
        canonical_monitoring_platform="unknown",
        availability="manual_only",
        default_enabled=False,
    ),
    # Kimi 退出默认:契约无 kimi 专属 surface 枚举 → 归 other_explicit,真实身份由
    # provider_key/model_key/product_label 携带(见 §10 上报的契约缺口)。
    "other_explicit": SurfaceSpec(
        surface_key="other_explicit",
        platform_key="kimi",
        product_label="Kimi",
        provider_key="dashscope",  # 研究链 Kimi 经 DashScope compat-mode
        default_model_key="kimi-k2.6",
        canonical_monitoring_platform="kimi",
        env_key_var="DASHSCOPE_API_KEY",
        model_config_key="model_kimi_via_dashscope",
        availability="active",
        default_enabled=False,      # 退出新售默认
        legacy_read_only=True,      # 历史只读保留,不删
        default_search_enabled=True,
        search_provider="dashscope-search",
    ),
}

# 完整性自检:每个 surface_key 枚举都必须有 spec(反之 other_explicit 复用给 Kimi)。
_missing = set(SURFACE_KEYS) - set(SURFACE_SPECS)
assert not _missing, f"SURFACE_SPECS 缺少 surface_key: {_missing}"


# ---------------------------------------------------------------------------
# 只读查询 helper
# ---------------------------------------------------------------------------


def get_surface_spec(surface_key: str) -> SurfaceSpec:
    spec = SURFACE_SPECS.get(surface_key)
    if spec is None:
        raise KeyError(f"未知 surface_key: {surface_key!r}(非机器契约枚举)")
    return spec


# ---------------------------------------------------------------------------
# 表面替换(复审 P1-4):policy 选中但 AI-1 无可用 API 的表面 → 替换到该平台**诚实可用**的表面,
# 绝不静默丢平台。deepseek_native_with_search(官方无可验证原生搜索,Codex #3 标 unavailable)
# 而 AI-2 默认矩阵却选它 → 替换到 deepseek_dashscope_search_legacy(百炼,带搜索,provider=dashscope
# 诚实标注,非冒名),使 DeepSeek 仍实跑。此为**已上报的跨包冲突临时处置**,最终以 boss/Codex 裁定为准。
# ---------------------------------------------------------------------------

# [2026-07-27] 原有一条 deepseek_native_with_search → deepseek_dashscope_search_legacy
#   的替换已**删除**,两个理由:
#   ① native_with_search 已实测转 active,不再需要替身;
#   ② 更要紧的是工单红线:官方通道不可用时**绝不静默回落**到阿里 ——
#      那正是"我们以为在测 DeepSeek,实际测的是阿里"这个坑的成因。
#      官方挂了就让它显式 fail-closed(readiness=attention_required),由人来决定怎么办。
#   机制保留(空表):将来若有**同供应商**的合理替身再登记。
SURFACE_SUBSTITUTIONS: Dict[str, str] = {}


def resolve_runtime_surface(surface_key: str) -> Tuple[str, bool]:
    """把 policy 选中的表面解析为**运行时实跑**表面。

    返回 (runtime_surface_key, substituted)。若选中表面 active → 原样;若不可用且有替换 → 替换(True);
    若不可用且无替换 → 原样(False,由 readiness/matrix 标出,不静默)。
    """
    spec = SURFACE_SPECS.get(surface_key)
    if spec is not None and spec.availability == "active":
        return surface_key, False
    sub = SURFACE_SUBSTITUTIONS.get(surface_key)
    if sub is not None and SURFACE_SPECS.get(sub) is not None and SURFACE_SPECS[sub].availability == "active":
        return sub, True
    return surface_key, False


def default_matrix_surfaces() -> Tuple[str, ...]:
    """新售默认全量矩阵的 surface_key(豆包/千问/DeepSeek 原生/元宝 Hy3)。"""
    return tuple(k for k, s in SURFACE_SPECS.items() if s.default_enabled)


def canonical_monitoring_platform(surface_key: str) -> str:
    """把 surface_key 映射到 db.monitoring_db.PLATFORM_WEIGHTS 的 canonical 平台名。

    agent 复用地图 gotcha:yuanbao_hy3_tokenhub / qwen_dashscope_search / other_explicit(kimi)
    这些名字 normalize_monitoring_platform 不认识 → 必须先经本映射再取权重,否则等权回落。
    """
    return get_surface_spec(surface_key).canonical_monitoring_platform


def is_kimi_surface(surface_key: str) -> bool:
    return get_surface_spec(surface_key).platform_key == "kimi"
