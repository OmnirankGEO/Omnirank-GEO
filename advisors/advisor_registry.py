"""
顾问注册中心
管理所有顾问的创建、获取、列表
"""

import os
import sys
import json
from typing import Optional
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .base_advisor import BaseAdvisor

# 2026-05-11 老板拍板:社媒蒸馏 + 小榜对话默认换 deepseek-v4-flash(替代 qwen3-max)
# 影响范围:
#   · POST /api/social/profile/ai-fill(社媒资料蒸馏 · 默认 advisor=huang-douyin)
#   · advisor_chat() 默认调 huang-douyin(小榜对话主入口)
#   · 任何手动指定 advisor_id='huang-douyin' 的场景(含 社媒工作台 其他选题/拆解链路)
# 强制覆盖 prod DB advisors.model_name 字段 · 不依赖 SQL migration · 重启即生效
# 改回 qwen3-max:删本字典或 DB 改 model_name='qwen3-max'(代码 fallback 优先级低于本 override)
_SOCIAL_ADVISOR_MODEL_OVERRIDES: dict[str, tuple[str, str]] = {
    # advisor_id: (model_name, api_provider)
    "huang-douyin": ("deepseek-v4-flash", "dashscope"),
}


class AdvisorRegistry:
    """
    顾问注册中心 - 单例模式
    
    负责：
    1. 管理所有已创建的顾问
    2. 从数据库/配置加载顾问
    3. 创建新顾问
    """
    
    _instance = None
    _advisors: dict[str, BaseAdvisor] = {}
    
    @classmethod
    def get_instance(cls) -> "AdvisorRegistry":
        if cls._instance is None:
            cls._instance = cls()
            cls._instance._load_advisors()
        return cls._instance
    
    def _load_advisors(self):
        """从数据库加载已有顾问"""
        try:
            from db.diagnosis_db import get_advisors
            advisors_data = get_advisors()
            self._advisors = {}
            
            for data in advisors_data:
                advisor_id = data["id"]
                api_provider = data.get("api_provider", "dashscope")
                model_name = data.get("model_name", data.get("model_id", "qwen3.7-max"))
                # 2026-05-11 社媒侧 model override(老板拍板)· 见模块顶部 _SOCIAL_ADVISOR_MODEL_OVERRIDES
                if advisor_id in _SOCIAL_ADVISOR_MODEL_OVERRIDES:
                    forced_model, forced_provider = _SOCIAL_ADVISOR_MODEL_OVERRIDES[advisor_id]
                    if model_name != forced_model or api_provider != forced_provider:
                        print(f"  [社媒 override] {advisor_id}: {api_provider}/{model_name} → {forced_provider}/{forced_model}")
                        model_name = forced_model
                        api_provider = forced_provider

                advisor = BaseAdvisor(
                    advisor_id=advisor_id,
                    name=data["name"],
                    avatar=data.get("avatar", "👤"),
                    description=data.get("description", ""),
                    base_prompt=data.get("base_prompt", ""),
                    api_provider=api_provider,
                    model_name=model_name,
                )
                # 加载已有文档
                advisor.load_documents()
                self._advisors[advisor_id] = advisor
                advisor.role = data.get("role", "writer")
                print(f"  [OK] 加载顾问: {data['name']} ({advisor_id}) · {api_provider}/{model_name}")
        except Exception as e:
            print(f"  [WARN] 加载顾问失败: {e}")
    
    def get(self, advisor_id: str) -> Optional[BaseAdvisor]:
        """获取顾问实例"""
        return self._advisors.get(advisor_id)
    
    def list_all(self) -> list[dict]:
        """列出所有顾问"""
        result = []
        for advisor in self._advisors.values():
            profile = advisor.get_profile()
            profile["role"] = getattr(advisor, "role", "writer")
            result.append(profile)
        return result
    
    def create(
        self,
        advisor_id: str,
        name: str,
        base_prompt: str,
        avatar: str = "👤",
        description: str = "",
        model: str = "qwen3.7-max",
    ) -> BaseAdvisor:
        """创建新顾问"""
        # 创建顾问实例
        advisor = BaseAdvisor(
            advisor_id=advisor_id,
            name=name,
            avatar=avatar,
            description=description,
            base_prompt=base_prompt,
            model_name=model,
        )
        
        # 保存到数据库
        try:
            from db.diagnosis_db import save_advisor
            save_advisor(
                advisor_id=advisor_id,
                name=name,
                avatar=avatar,
                description=description,
                base_prompt=base_prompt,
                model_id=model,
            )
        except Exception as e:
            print(f"  [WARN] 保存顾问到数据库失败: {e}")
        
        # 注册到内存
        self._advisors[advisor_id] = advisor
        
        return advisor
    
    def delete(self, advisor_id: str) -> bool:
        """删除顾问"""
        if advisor_id in self._advisors:
            del self._advisors[advisor_id]
            
            try:
                from db.diagnosis_db import delete_advisor
                delete_advisor(advisor_id)
            except Exception as e:
                print(f"  [WARN] 从数据库删除顾问失败: {e}")
            
            return True
        return False


# 便捷函数
def get_advisor(advisor_id: str) -> Optional[BaseAdvisor]:
    """获取顾问实例"""
    return AdvisorRegistry.get_instance().get(advisor_id)


def list_advisors() -> list[dict]:
    """列出所有顾问"""
    return AdvisorRegistry.get_instance().list_all()


def create_advisor(**kwargs) -> BaseAdvisor:
    """创建顾问"""
    return AdvisorRegistry.get_instance().create(**kwargs)
