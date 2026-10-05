"""
Prompt模板管理系统
支持版本控制、A/B测试赛马、评分历史追踪
"""
import os
import json
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict, field

# ============ 数据模型 ============

@dataclass
class PromptVersion:
    """Prompt版本"""
    version_id: str
    version_name: str  # 如 "v2.0", "v3.1"
    prompt_content: str
    created_at: str
    is_active: bool = False
    avg_score: float = 0.0
    test_count: int = 0
    notes: str = ""
    
@dataclass
class PromptTemplate:
    """Prompt模板"""
    template_id: str
    template_name: str  # 如 "榜单测评型", "趋势洞察型"
    template_code: str  # 如 "ranking_list", "trend_insight"
    category: str  # 如 "第一梯队", "第二梯队"
    description: str
    current_version_id: str  # 当前生产版本
    versions: List[PromptVersion] = field(default_factory=list)
    target_score: int = 88  # 目标分数
    created_at: str = ""
    updated_at: str = ""
    
@dataclass
class ABTest:
    """A/B测试赛马"""
    test_id: str
    test_name: str
    template_id: str
    version_a_id: str
    version_b_id: str
    status: str  # "running", "completed", "paused"
    start_time: str
    end_time: Optional[str] = None
    version_a_results: List[Dict] = field(default_factory=list)
    version_b_results: List[Dict] = field(default_factory=list)
    winner: Optional[str] = None

@dataclass
class ScoreRecord:
    """评分记录"""
    record_id: str
    template_id: str
    version_id: str
    article_title: str
    score: int
    dimension_scores: Dict
    review_text: str
    created_at: str
    reviewer_model: str = "gemini-3-pro-preview"


# ============ 模板管理器 ============

class PromptTemplateManager:
    """Prompt模板管理器"""
    
    def __init__(self, data_dir: str = "data/prompt_templates"):
        self.data_dir = data_dir
        self.templates_file = os.path.join(data_dir, "templates.json")
        self.scores_file = os.path.join(data_dir, "score_history.json")
        self.abtests_file = os.path.join(data_dir, "ab_tests.json")
        
        # 确保目录存在
        os.makedirs(data_dir, exist_ok=True)
        
        # 加载数据
        self.templates: Dict[str, PromptTemplate] = {}
        self.score_history: List[ScoreRecord] = []
        self.ab_tests: Dict[str, ABTest] = {}
        
        self._load_data()
    
    def _load_data(self):
        """加载持久化数据"""
        # 加载模板
        if os.path.exists(self.templates_file):
            with open(self.templates_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for t_data in data.get("templates", []):
                    versions = [PromptVersion(**v) for v in t_data.pop("versions", [])]
                    template = PromptTemplate(**t_data, versions=versions)
                    self.templates[template.template_id] = template
        
        # 加载评分历史
        if os.path.exists(self.scores_file):
            with open(self.scores_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.score_history = [ScoreRecord(**s) for s in data.get("scores", [])]
        
        # 加载A/B测试
        if os.path.exists(self.abtests_file):
            with open(self.abtests_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for ab_data in data.get("ab_tests", []):
                    ab_test = ABTest(**ab_data)
                    self.ab_tests[ab_test.test_id] = ab_test
    
    def _save_templates(self):
        """保存模板数据"""
        data = {
            "templates": [asdict(t) for t in self.templates.values()],
            "updated_at": datetime.now().isoformat()
        }
        with open(self.templates_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _save_scores(self):
        """保存评分历史"""
        data = {
            "scores": [asdict(s) for s in self.score_history],
            "updated_at": datetime.now().isoformat()
        }
        with open(self.scores_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _save_abtests(self):
        """保存A/B测试"""
        data = {
            "ab_tests": [asdict(ab) for ab in self.ab_tests.values()],
            "updated_at": datetime.now().isoformat()
        }
        with open(self.abtests_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    # ========== 模板CRUD ==========
    
    def create_template(
        self,
        template_name: str,
        template_code: str,
        category: str,
        description: str,
        initial_prompt: str,
        version_name: str = "v1.0"
    ) -> PromptTemplate:
        """创建新模板"""
        template_id = str(uuid.uuid4())[:8]
        version_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        
        version = PromptVersion(
            version_id=version_id,
            version_name=version_name,
            prompt_content=initial_prompt,
            created_at=now,
            is_active=True
        )
        
        template = PromptTemplate(
            template_id=template_id,
            template_name=template_name,
            template_code=template_code,
            category=category,
            description=description,
            current_version_id=version_id,
            versions=[version],
            created_at=now,
            updated_at=now
        )
        
        self.templates[template_id] = template
        self._save_templates()
        return template
    
    def get_template(self, template_id: str) -> Optional[PromptTemplate]:
        """获取模板"""
        return self.templates.get(template_id)
    
    def get_template_by_code(self, code: str) -> Optional[PromptTemplate]:
        """根据code获取模板"""
        for t in self.templates.values():
            if t.template_code == code:
                return t
        return None
    
    def list_templates(self, category: str = None) -> List[PromptTemplate]:
        """列出所有模板"""
        templates = list(self.templates.values())
        if category:
            templates = [t for t in templates if t.category == category]
        return templates
    
    def update_template(self, template_id: str, **kwargs) -> Optional[PromptTemplate]:
        """更新模板基本信息"""
        template = self.templates.get(template_id)
        if not template:
            return None
        
        for key, value in kwargs.items():
            if hasattr(template, key) and key not in ["template_id", "versions", "created_at"]:
                setattr(template, key, value)
        
        template.updated_at = datetime.now().isoformat()
        self._save_templates()
        return template
    
    def delete_template(self, template_id: str) -> bool:
        """删除模板"""
        if template_id in self.templates:
            del self.templates[template_id]
            self._save_templates()
            return True
        return False
    
    # ========== 版本管理 ==========
    
    def add_version(
        self,
        template_id: str,
        version_name: str,
        prompt_content: str,
        notes: str = "",
        set_active: bool = False
    ) -> Optional[PromptVersion]:
        """添加新版本"""
        template = self.templates.get(template_id)
        if not template:
            return None
        
        version_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        
        version = PromptVersion(
            version_id=version_id,
            version_name=version_name,
            prompt_content=prompt_content,
            created_at=now,
            is_active=set_active,
            notes=notes
        )
        
        if set_active:
            # 取消其他版本的激活状态
            for v in template.versions:
                v.is_active = False
            template.current_version_id = version_id
        
        template.versions.append(version)
        template.updated_at = now
        self._save_templates()
        return version
    
    def get_version(self, template_id: str, version_id: str) -> Optional[PromptVersion]:
        """获取指定版本"""
        template = self.templates.get(template_id)
        if not template:
            return None
        
        for v in template.versions:
            if v.version_id == version_id:
                return v
        return None
    
    def get_active_version(self, template_id: str) -> Optional[PromptVersion]:
        """获取当前激活版本"""
        template = self.templates.get(template_id)
        if not template:
            return None
        
        for v in template.versions:
            if v.version_id == template.current_version_id:
                return v
        return None
    
    def set_active_version(self, template_id: str, version_id: str) -> bool:
        """设置激活版本（投入生产）"""
        template = self.templates.get(template_id)
        if not template:
            return False
        
        for v in template.versions:
            v.is_active = (v.version_id == version_id)
        
        template.current_version_id = version_id
        template.updated_at = datetime.now().isoformat()
        self._save_templates()
        return True
    
    def get_prompt_content(self, template_code: str) -> Optional[str]:
        """获取模板当前激活版本的Prompt内容"""
        template = self.get_template_by_code(template_code)
        if not template:
            return None
        
        version = self.get_active_version(template.template_id)
        if not version:
            return None
        
        return version.prompt_content
    
    # ========== A/B测试赛马 ==========
    
    def create_ab_test(
        self,
        test_name: str,
        template_id: str,
        version_a_id: str,
        version_b_id: str
    ) -> ABTest:
        """创建A/B测试"""
        test_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        
        ab_test = ABTest(
            test_id=test_id,
            test_name=test_name,
            template_id=template_id,
            version_a_id=version_a_id,
            version_b_id=version_b_id,
            status="running",
            start_time=now
        )
        
        self.ab_tests[test_id] = ab_test
        self._save_abtests()
        return ab_test
    
    def record_ab_result(
        self,
        test_id: str,
        version_id: str,
        score: int,
        article_title: str
    ) -> bool:
        """记录A/B测试结果"""
        ab_test = self.ab_tests.get(test_id)
        if not ab_test or ab_test.status != "running":
            return False
        
        result = {
            "score": score,
            "article_title": article_title,
            "timestamp": datetime.now().isoformat()
        }
        
        if version_id == ab_test.version_a_id:
            ab_test.version_a_results.append(result)
        elif version_id == ab_test.version_b_id:
            ab_test.version_b_results.append(result)
        else:
            return False
        
        self._save_abtests()
        return True
    
    def complete_ab_test(self, test_id: str) -> Optional[str]:
        """完成A/B测试并确定获胜者"""
        ab_test = self.ab_tests.get(test_id)
        if not ab_test:
            return None
        
        # 计算平均分
        avg_a = sum(r["score"] for r in ab_test.version_a_results) / len(ab_test.version_a_results) if ab_test.version_a_results else 0
        avg_b = sum(r["score"] for r in ab_test.version_b_results) / len(ab_test.version_b_results) if ab_test.version_b_results else 0
        
        ab_test.status = "completed"
        ab_test.end_time = datetime.now().isoformat()
        ab_test.winner = ab_test.version_a_id if avg_a >= avg_b else ab_test.version_b_id
        
        self._save_abtests()
        return ab_test.winner
    
    def get_ab_test_stats(self, test_id: str) -> Optional[Dict]:
        """获取A/B测试统计"""
        ab_test = self.ab_tests.get(test_id)
        if not ab_test:
            return None
        
        return {
            "test_id": ab_test.test_id,
            "test_name": ab_test.test_name,
            "status": ab_test.status,
            "version_a": {
                "version_id": ab_test.version_a_id,
                "count": len(ab_test.version_a_results),
                "avg_score": sum(r["score"] for r in ab_test.version_a_results) / len(ab_test.version_a_results) if ab_test.version_a_results else 0
            },
            "version_b": {
                "version_id": ab_test.version_b_id,
                "count": len(ab_test.version_b_results),
                "avg_score": sum(r["score"] for r in ab_test.version_b_results) / len(ab_test.version_b_results) if ab_test.version_b_results else 0
            },
            "winner": ab_test.winner
        }
    
    # ========== 评分历史 ==========
    
    def record_score(
        self,
        template_id: str,
        version_id: str,
        article_title: str,
        score: int,
        dimension_scores: Dict,
        review_text: str,
        reviewer_model: str = "gemini-3-pro-preview"
    ) -> ScoreRecord:
        """记录评分"""
        record_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        
        record = ScoreRecord(
            record_id=record_id,
            template_id=template_id,
            version_id=version_id,
            article_title=article_title,
            score=score,
            dimension_scores=dimension_scores,
            review_text=review_text,
            created_at=now,
            reviewer_model=reviewer_model
        )
        
        self.score_history.append(record)
        
        # 更新版本的平均分
        template = self.templates.get(template_id)
        if template:
            for v in template.versions:
                if v.version_id == version_id:
                    v.test_count += 1
                    v.avg_score = (v.avg_score * (v.test_count - 1) + score) / v.test_count
                    break
            self._save_templates()
        
        self._save_scores()
        return record
    
    def get_score_history(
        self,
        template_id: str = None,
        version_id: str = None,
        limit: int = 50
    ) -> List[ScoreRecord]:
        """获取评分历史"""
        records = self.score_history
        
        if template_id:
            records = [r for r in records if r.template_id == template_id]
        if version_id:
            records = [r for r in records if r.version_id == version_id]
        
        return sorted(records, key=lambda x: x.created_at, reverse=True)[:limit]
    
    def get_version_stats(self, template_id: str, version_id: str) -> Dict:
        """获取版本统计数据"""
        records = self.get_score_history(template_id, version_id, limit=1000)
        
        if not records:
            return {"count": 0, "avg_score": 0, "max_score": 0, "min_score": 0}
        
        scores = [r.score for r in records]
        return {
            "count": len(scores),
            "avg_score": sum(scores) / len(scores),
            "max_score": max(scores),
            "min_score": min(scores),
            "pass_rate": len([s for s in scores if s >= 88]) / len(scores) * 100
        }


# ============ 初始化默认模板 ============

def init_default_templates(manager: PromptTemplateManager):
    """初始化默认模板（历史代码保留，语义改为证据选型）。"""
    from writing.templates.evidence_ranking_template import EVIDENCE_RANKING_PROMPT
    
    # 检查是否已存在
    if manager.get_template_by_code("ranking_list_v2"):
        return
    
    # 历史模板 code 不能删除，否则旧记录无法读取；新建内容不再生成榜单。
    manager.create_template(
        template_name="证据选型型 v3.0",
        template_code="ranking_list_v2",
        category="证据内容",
        description="排名/推荐依据、同口径证据表、适用场景、限制条件和读者核验步骤",
        initial_prompt=EVIDENCE_RANKING_PROMPT,
        version_name="v3.0"
    )
    
    # V9版本需要单独读取
    v9_path = "knowledge/案例库/v9_ranking_template.md"
    if os.path.exists(v9_path):
        with open(v9_path, "r", encoding="utf-8") as f:
            v9_prompt = f.read()
        
        manager.create_template(
            template_name="行业深度模板 V9（历史兼容）",
            template_code="ranking_list_v9",
            category="第一梯队",
            description="旧模板代码兼容入口；运行时会叠加证据优先总契约",
            initial_prompt=EVIDENCE_RANKING_PROMPT,
            version_name="v9.0"
        )


# ============ 全局实例 ============
_manager_instance = None

def get_template_manager() -> PromptTemplateManager:
    """获取模板管理器单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = PromptTemplateManager()
    return _manager_instance
