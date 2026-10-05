"""
GEO 诊断状态管理
支持断点续跑、检查点保存、自动重试
"""

import json
import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional


class DiagnosisState:
    """诊断状态持久化管理"""
    
    def __init__(self, session_id: str, cache_dir: str = "cache"):
        self.session_id = session_id
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / f"{session_id}.json"
        self._state = self._load()
    
    def _load(self) -> dict:
        """加载已有状态"""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def _save(self):
        """保存状态到磁盘"""
        with open(self.cache_file, 'w', encoding='utf-8') as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)
    
    def save_checkpoint(self, step: str, data: Any):
        """保存检查点"""
        self._state[step] = {
            "data": data,
            "timestamp": datetime.now().isoformat(),
            "status": "completed"
        }
        self._save()
        print(f"  💾 检查点已保存: {step}")
    
    def get_checkpoint(self, step: str) -> Optional[Any]:
        """获取检查点数据"""
        checkpoint = self._state.get(step)
        if checkpoint and checkpoint.get("status") == "completed":
            print(f"  📂 从检查点恢复: {step}")
            return checkpoint.get("data")
        return None
    
    def has_checkpoint(self, step: str) -> bool:
        """检查是否有检查点"""
        return step in self._state and self._state[step].get("status") == "completed"
    
    def clear(self):
        """清除所有检查点"""
        self._state = {}
        if self.cache_file.exists():
            os.remove(self.cache_file)


async def retry_async(
    func: Callable,
    *args,
    max_retries: int = 3,
    delay: float = 1.0,
    **kwargs
) -> Any:
    """异步重试装饰器"""
    last_error = None
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = delay * (attempt + 1)
                print(f"  ⚠️ 重试 {attempt + 1}/{max_retries}，等待 {wait_time}s...")
                await asyncio.sleep(wait_time)
    raise last_error


async def parallel_gather(tasks: list, return_exceptions: bool = True) -> list:
    """并行执行多个任务，自动处理异常"""
    results = await asyncio.gather(*tasks, return_exceptions=return_exceptions)
    
    # 统计成功/失败
    success_count = sum(1 for r in results if not isinstance(r, Exception))
    fail_count = len(results) - success_count
    
    if fail_count > 0:
        print(f"  ⚠️ 并行任务: {success_count} 成功, {fail_count} 失败")
    else:
        print(f"  ✅ 并行任务: 全部 {success_count} 个成功")
    
    return results
