"""
社媒操盘手统一LLM入口 — 全部通过顾问驱动

默认顾问: 社恐小黄 (huang-douyin)
特点:
- advisor_chat(): 带知识库RAG + 角色扮演，适合内容生成/分析
- advisor_generate(): 使用顾问模型但不注入角色，适合JSON结构化输出
- 全部自动回退到 call_llm_with_fallback
"""

import logging
from typing import Optional

logger = logging.getLogger("GEO-AdvisorLLM")

DEFAULT_ADVISOR_ID = "huang-douyin"  # 社恐小黄


def _get_advisor(advisor_id: str = None):
    """获取顾问实例（懒加载，避免循环导入）"""
    try:
        from advisors.advisor_registry import get_advisor
        advisor = get_advisor(advisor_id or DEFAULT_ADVISOR_ID)
        if advisor:
            return advisor
        logger.debug(f"顾问 {advisor_id or DEFAULT_ADVISOR_ID} 未找到")
    except Exception as e:
        logger.debug(f"获取顾问失败: {e}")
    return None


async def advisor_chat(message: str, context: str = "", advisor_id: str = None) -> str:
    """
    通过顾问对话（带RAG知识库检索 + 角色扮演）
    适用场景: 内容生成、拆解分析、脚本创作

    Args:
        message: 用户消息/提示词
        context: 额外上下文
        advisor_id: 指定顾问，默认社恐小黄
    Returns:
        LLM生成的文本
    """
    advisor = _get_advisor(advisor_id)
    if advisor:
        try:
            result = await advisor.chat(message, context=context)
            response = result.get("response", "")
            if response and not response.startswith("[错误") and not response.startswith("[调用失败"):
                return response
            logger.warning(f"顾问返回异常: {response[:80]}")
        except Exception as e:
            logger.warning(f"顾问对话失败: {e}")

    return await _fallback_llm(message)


async def advisor_generate(prompt: str, advisor_id: str = None, temperature: float = None) -> str:
    """
    通过顾问模型直接生成（不注入角色/知识库）
    适用场景: JSON结构化输出、精确指令、数据提取

    Args:
        prompt: 完整提示词
        advisor_id: 指定顾问，默认社恐小黄
        temperature: 可选覆盖温度
    Returns:
        LLM原始输出
    """
    advisor = _get_advisor(advisor_id)
    if advisor:
        try:
            old_temp = advisor.temperature
            if temperature is not None:
                advisor.temperature = temperature
            try:
                response = await advisor._call_llm(prompt)
            finally:
                advisor.temperature = old_temp
            if response and not response.startswith("[错误") and not response.startswith("[调用失败"):
                return response
            logger.warning(f"顾问LLM返回异常: {response[:80]}")
        except Exception as e:
            logger.warning(f"顾问LLM调用失败: {e}")

    return await _fallback_llm(prompt)


async def advisor_generate_with_search(prompt: str, advisor_id: str = None, temperature: float = None) -> str:
    """
    带联网搜索的LLM生成 — 遇到不认识的术语时可以搜索
    用于采访对话等需要理解行业新词的场景
    """
    advisor = _get_advisor(advisor_id)
    if advisor:
        try:
            old_temp = advisor.temperature
            old_search = advisor.enable_web_search
            if temperature is not None:
                advisor.temperature = temperature
            advisor.enable_web_search = True
            try:
                response = await advisor._call_llm(prompt)
            finally:
                advisor.temperature = old_temp
                advisor.enable_web_search = old_search
            if response and not response.startswith("[错误") and not response.startswith("[调用失败"):
                return response
            logger.warning(f"联网LLM返回异常: {response[:80]}")
        except Exception as e:
            logger.warning(f"联网LLM调用失败，降级到普通模式: {e}")

    # 降级到无联网版本
    return await advisor_generate(prompt, advisor_id=advisor_id, temperature=temperature)


async def advisor_flash(prompt: str, history: list[dict] = None) -> str:
    """
    Flash 级别 LLM 调用（qwen-turbo，极低成本）
    适用场景: 资料收集对话、意图分类、字段提取 — 不需要高质量创作的轻量任务

    Args:
        prompt: system prompt 或完整提示词
        history: 可选的对话历史 [{"role": "user/assistant", "content": "..."}]
    Returns:
        LLM 原始输出
    """
    import os
    import httpx
    from tools.llm_call_tracker import llm_track, usage_from_response_payload

    flash_provider = os.getenv("SOCIAL_FLASH_PROVIDER", "dashscope").strip().lower()
    if flash_provider in ("deepseek", "deepseek-v4", "deepseek-v4-flash"):
        try:
            from services.llm.deepseek_key_pool import pick_deepseek_api_key

            api_key = pick_deepseek_api_key("realtime")
        except Exception:
            api_key = os.getenv("DEEPSEEK_API_KEY")
        if api_key:
            messages = [{"role": "system", "content": prompt}]
            if history:
                messages.extend(history)
            try:
                model_name = os.getenv("SOCIAL_FLASH_MODEL", "deepseek-v4-flash")
                async with httpx.AsyncClient(timeout=45.0) as client:
                    async with llm_track(
                        "advisor_flash",
                        "deepseek",
                        model=model_name,
                        metadata={"provider": "deepseek"},
                    ) as tracker:
                        resp = await client.post(
                            "https://api.deepseek.com/v1/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json={
                                "model": model_name,
                                "messages": messages,
                                "temperature": 0.2,
                                "max_tokens": 900,
                                "stream": False,
                                "thinking": {"type": "disabled"},
                            },
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                            tracker.record(
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                cached_tokens=cached_tokens,
                                success=True,
                            )
                        else:
                            tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        return content
                    logger.warning(f"[Flash:DeepSeek] 返回空内容: {data}")
                else:
                    logger.warning(f"[Flash:DeepSeek] API 错误 {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                logger.warning(f"[Flash:DeepSeek] 调用失败，降级到 DashScope: {e}")
        else:
            logger.warning("[Flash:DeepSeek] DEEPSEEK_API_KEY 未配置，降级到 DashScope")

    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        logger.warning("[Flash] DASHSCOPE_API_KEY 未配置，降级到 advisor_generate")
        return await advisor_generate(prompt)

    messages = [{"role": "system", "content": prompt}]
    if history:
        messages.extend(history)

    # 主 Key + 备用 Key 自动切换
    keys = [api_key]
    backup = os.getenv("DASHSCOPE_API_KEY_BACKUP")
    if backup:
        keys.append(backup)

    for key in keys:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with llm_track(
                    "advisor_flash",
                    "dashscope",
                    model="qwen3.6-flash",
                    metadata={"backup_key": key != api_key},
                ) as tracker:
                    resp = await client.post(
                        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={
                            "model": "qwen3.6-flash",
                            "messages": messages,
                            "temperature": 0.5,
                            "max_tokens": 800,
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)
                        tracker.record(
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_tokens=cached_tokens,
                            success=True,
                        )
                    else:
                        tracker.record(success=False, error_msg=f"HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        return content
                    logger.warning(f"[Flash] 返回空内容: {data}")
                elif resp.status_code in (429, 503):
                    logger.warning(f"[Flash] {resp.status_code} 限流，切换备用Key")
                    continue
                else:
                    logger.warning(f"[Flash] API 错误 {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"[Flash] 调用失败: {e}，尝试备用Key")
            continue

    # 全部失败，降级到 advisor_generate
    return await advisor_generate(prompt)


async def _fallback_llm(prompt: str) -> str:
    """回退到通用多模型调用"""
    try:
        from tools.multi_llm_caller import call_llm_with_fallback
        result = await call_llm_with_fallback(prompt, verbose=False)
        return result or ""
    except Exception as e:
        logger.error(f"回退LLM也失败: {e}")
        return ""
