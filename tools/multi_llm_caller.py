"""
多API后备LLM调用模块
优先级: DeepSeek官方 → DashScope(阿里云) → SiliconFlow(硅基流动)

[CTO-15.23 2026-05-09 Kimi 月烧 ¥1500 治理]
- Kimi 从 fallback 链中移除(它最贵 · ¥4/M in + ¥21/M out · 不应作为通用 fallback)
- Kimi 仅保留诊断/监测的 $web_search 必用场景(直连 · 不走此链)
- call_kimi() 历史接口改为调 deepseek-v4-flash(周报/选题不需 web_search)

[CTO-15.23 2026-05-05] 全链路升级到 DeepSeek V4-Flash:
- V4-Flash 实测准确率 100% (V4-Pro 86%, 反直觉但成立 · 见 scripts/cluster_v4_flash_vs_pro.py)
- 价格 ¥1/M in + ¥2/M out · 比 V3.2 便宜 + 比 V4-Pro 便宜 12.5x
- thinking=OFF 走极速路径 · 单次延迟 ~3-4s · 适合聚类/分类/JSON 输出场景
- 长文/复杂推理任务自行单独直连 V4-Pro 不走本 fallback 链
"""
import asyncio
from config.deepseek_models import DEEPSEEK_OFFICIAL_FLASH
import inspect
import os
import httpx
from typing import Callable, Optional, Tuple
from dotenv import load_dotenv
from tools.llm_call_tracker import infer_platform_from_url, llm_track, usage_from_response_payload

load_dotenv()


class ProviderCallGuardRejected(RuntimeError):
    """A live authority/lease guard stopped a provider call before I/O."""


class MultiLLMCaller:
    """多API后备LLM调用器"""

    # API配置 · 全链路 V4-Flash + thinking=OFF (2026-05-05 彻底废弃 V3.2)
    PROVIDERS = [
        {
            "name": "DeepSeek官方",
            "url": "https://api.deepseek.com/v1/chat/completions",
            "model": DEEPSEEK_OFFICIAL_FLASH,
            "env_key": "DEEPSEEK_API_KEY",
            "max_tokens": 8000,
            "extra_params": {"thinking": {"type": "disabled"}},
        },
        {
            "name": "DashScope(阿里云)",
            "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "model": "deepseek-v4-flash",
            "env_key": "DASHSCOPE_API_KEY",
            "max_tokens": 16000,
            "extra_params": {"thinking": {"type": "disabled"}},
        },
        # 2026-05-22 老板拍板:V3.2 → V4 全切 · SiliconFlow V3.2 兜底删除
        # SiliconFlow 暂未提供 V4-Flash · 等 SiliconFlow 上 V4 再加回 · 现 2 级兜底已足
        # Kimi 已从 fallback 链移除(2026-05-09)· 它最贵不应作为通用兜底
        # 诊断/监测的 $web_search 硬场景请直连 api.moonshot.cn · 不走此链
    ]

    def __init__(self, timeout: float = 300.0, temperature: float = 0.6):
        self.timeout = timeout
        self.temperature = temperature
    
    async def _call_provider(self, provider: dict, prompt: str) -> Tuple[bool, str]:
        """调用单个API提供商"""
        api_key = os.getenv(provider["env_key"])
        if not api_key:
            return False, f"未配置{provider['env_key']}"
        
        try:
            platform = infer_platform_from_url(provider["url"])
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                payload = {
                    "model": provider["model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.temperature,
                    "max_tokens": provider["max_tokens"],
                }

                # 添加额外参数
                if "extra_params" in provider:
                    payload.update(provider["extra_params"])

                async with llm_track(
                    "multi_llm_fallback",
                    platform,
                    model=provider["model"],
                    metadata={"provider_name": provider["name"]},
                ) as tracker:
                    response = await client.post(
                        provider["url"],
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"
                        },
                        json=payload
                    )

                    if response.status_code == 200:
                        result = response.json()
                        input_tokens, output_tokens, cached_tokens = usage_from_response_payload(result)
                        tracker.record(
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_tokens=cached_tokens,
                            success=True,
                        )
                        content = result["choices"][0]["message"]["content"]
                        return True, content

                    tracker.record(success=False, error_msg=f"HTTP {response.status_code}: {response.text[:200]}")
                    return False, f"HTTP {response.status_code}: {response.text[:200]}"
                    
        except httpx.TimeoutException:
            return False, "请求超时"
        except Exception as e:
            return False, str(e)
    
    async def call(
        self,
        prompt: str,
        verbose: bool = True,
        before_provider_call: Optional[Callable[[], object]] = None,
    ) -> Tuple[str, str]:
        """
        调用LLM，自动fallback到下一个提供商
        
        Returns:
            Tuple[str, str]: (内容, 使用的提供商名称)
        """
        for provider in self.PROVIDERS:
            if before_provider_call is not None:
                try:
                    guarded = before_provider_call()
                    if inspect.isawaitable(guarded):
                        await guarded
                except Exception as exc:
                    # Control flow, not provider failure: never fall through to
                    # another paid provider after authority/lease revocation.
                    raise ProviderCallGuardRejected(str(exc)) from exc
            if verbose:
                print(f"  🔄 尝试 {provider['name']}...")
            
            success, result = await self._call_provider(provider, prompt)
            
            if success:
                if verbose:
                    print(f"  ✅ {provider['name']} 成功")
                return result, provider["name"]
            else:
                if verbose:
                    print(f"  ❌ {provider['name']} 失败: {result[:50]}")
        
        return "[所有API均失败]", "None"


# 便捷函数
async def call_llm_with_fallback(
    prompt: str,
    verbose: bool = True,
    before_provider_call: Optional[Callable[[], object]] = None,
) -> str:
    """便捷函数：调用LLM并自动fallback"""
    caller = MultiLLMCaller()
    content, provider = await caller.call(
        prompt, verbose, before_provider_call=before_provider_call,
    )
    return content


# [CTO-15.23 2026-05-09 Kimi 月烧 ¥1500 治理]
# call_kimi() 历史接口保留 · 内部改为调 deepseek-v4-flash 兜底链(老板拍板:Kimi 仅诊断/监测必用 web_search)
# 周报/选题生成不需要 web_search · 用 deepseek 既便宜又快
async def call_kimi(prompt: str, verbose: bool = True) -> str:
    """[已废弃 Kimi] 历史命名保留 · 改为走 deepseek 兜底链(deepseek-v4-flash)

    周报/选题生成等场景不需要 $web_search · Kimi 是浪费(¥4/M vs deepseek ¥1/M)。
    如需 $web_search · 直连 api.moonshot.cn(参考 server.py:15638 竞品调研模式)。
    """
    caller = MultiLLMCaller()
    if verbose:
        print(f"  🔄 [Kimi 已退役] 走 deepseek-v4-flash 兜底链...")
    content, provider = await caller.call(prompt, verbose)
    return content


# 测试
async def test_multi_llm():
    print("=" * 60)
    print("🧪 多API后备LLM测试")
    print("=" * 60)
    
    caller = MultiLLMCaller(timeout=60.0)
    
    prompt = "用一句话介绍什么是GEO（生成式引擎优化）"
    
    content, provider = await caller.call(prompt)
    
    print(f"\n📊 结果:")
    print(f"   提供商: {provider}")
    print(f"   回复: {content[:200]}...")
    
    return content, provider


if __name__ == "__main__":
    asyncio.run(test_multi_llm())
