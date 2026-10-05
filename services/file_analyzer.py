"""文件智能分析(PDF / Word / TXT 的信息提取)。

开源 E3 · B1b-1 · 2026-09-28 从社媒工具包(B2a 已整包删除)的同名文件上提,**逐字搬运**
`ExtractedInfo` / `FileAnalyzer` / `get_file_analyzer` / `analyze_file` / `analyze_text`;
两个 LLM 分析函数(只有社媒在用)留在原处随 E3 删。
"""

import os
import re
from typing import Dict, Any, Optional
from dataclasses import dataclass

# 尝试导入文件处理库
try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False

try:
    from docx import Document
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False


@dataclass
class ExtractedInfo:
    """提取的信息结构"""
    company_name: Optional[str] = None
    industry: Optional[str] = None
    business: Optional[str] = None
    target_users: Optional[str] = None
    products: Optional[str] = None
    pain_points: Optional[str] = None
    advantages: Optional[str] = None
    raw_text: str = ""


class FileAnalyzer:
    """文件分析器"""
    
    # 行业关键词映射
    INDUSTRY_KEYWORDS = {
        "教育": ["教育", "培训", "课程", "学习", "学校", "老师"],
        "电商": ["电商", "购物", "商城", "淘宝", "天猫", "京东", "直播带货"],
        "餐饮": ["餐饮", "美食", "餐厅", "外卖", "饭店", "厨师"],
        "美容": ["美容", "护肤", "化妆品", "医美", "美妆", "保养"],
        "健身": ["健身", "运动", "瑜伽", "减肥", "健身房", "私教"],
        "房产": ["房产", "楼盘", "地产", "置业", "房屋", "中介"],
        "婚庆": ["婚庆", "婚纱", "婚礼", "摄影", "结婚"],
        "母婴": ["母婴", "育儿", "宝宝", "亲子", "儿童"],
        "汽车": ["汽车", "车辆", "4S店", "买车", "新车"],
        "法律": ["律师", "法律", "法务", "诉讼", "案件"],
        "医疗": ["医疗", "医院", "诊所", "医生", "健康"],
        "金融": ["金融", "理财", "保险", "投资", "贷款"],
    }
    
    def __init__(self):
        pass
    
    def analyze_file(self, file_path: str) -> Dict[str, Any]:
        """分析文件并提取信息"""
        ext = os.path.splitext(file_path)[1].lower()
        
        # 提取文本
        if ext == ".pdf":
            text = self._extract_pdf(file_path)
        elif ext in [".docx", ".doc"]:
            text = self._extract_docx(file_path)
        elif ext == ".txt":
            text = self._extract_txt(file_path)
        else:
            return {"success": False, "error": f"不支持的文件格式: {ext}"}
        
        if not text:
            return {"success": False, "error": "无法提取文件内容"}
        
        # 分析提取的文本
        info = self._analyze_text(text)
        
        return {
            "success": True,
            "extracted": {
                "company_name": info.company_name,
                "industry": info.industry,
                "business": info.business,
                "target_users": info.target_users,
                "products": info.products,
                "pain_points": info.pain_points,
                "advantages": info.advantages,
            },
            "raw_text": info.raw_text[:2000],  # 限制长度
            "text_length": len(info.raw_text),
        }
    
    def analyze_text(self, text: str) -> Dict[str, Any]:
        """直接分析文本"""
        info = self._analyze_text(text)
        return {
            "success": True,
            "extracted": {
                "company_name": info.company_name,
                "industry": info.industry,
                "business": info.business,
                "target_users": info.target_users,
                "products": info.products,
                "pain_points": info.pain_points,
                "advantages": info.advantages,
            },
            "raw_text": info.raw_text[:2000],
        }
    
    def _extract_pdf(self, file_path: str) -> str:
        """提取PDF文本"""
        if not HAS_PYPDF2:
            return ""
        try:
            with open(file_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                text = ""
                for page in reader.pages:
                    text += page.extract_text() + "\n"
                return text
        except Exception as e:
            print(f"PDF提取失败: {e}")
            return ""
    
    def _extract_docx(self, file_path: str) -> str:
        """提取Word文本"""
        if not HAS_DOCX:
            return ""
        try:
            doc = Document(file_path)
            text = "\n".join([para.text for para in doc.paragraphs])
            return text
        except Exception as e:
            print(f"Word提取失败: {e}")
            return ""
    
    def _extract_txt(self, file_path: str) -> str:
        """提取TXT文本"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except UnicodeDecodeError:
            try:
                with open(file_path, 'r', encoding='gbk') as f:
                    return f.read()
            except:
                return ""
    
    def _analyze_text(self, text: str) -> ExtractedInfo:
        """分析文本提取结构化信息"""
        info = ExtractedInfo(raw_text=text)
        
        # 1. 提取公司名称
        info.company_name = self._extract_company_name(text)
        
        # 2. 识别行业
        info.industry = self._detect_industry(text)
        
        # 3. 提取业务描述
        info.business = self._extract_business(text)
        
        # 4. 提取目标用户
        info.target_users = self._extract_target_users(text)
        
        # 5. 提取产品信息
        info.products = self._extract_products(text)
        
        # 6. 提取痛点
        info.pain_points = self._extract_pain_points(text)
        
        # 7. 提取优势
        info.advantages = self._extract_advantages(text)
        
        return info
    
    def _extract_company_name(self, text: str) -> Optional[str]:
        """提取公司名称"""
        patterns = [
            r"公司名称[：:]\s*(.+?)(?:\n|$)",
            r"企业名称[：:]\s*(.+?)(?:\n|$)",
            r"品牌[：:]\s*(.+?)(?:\n|$)",
            r"(.+?有限公司)",
            r"(.+?股份公司)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()[:50]
        return None
    
    def _detect_industry(self, text: str) -> Optional[str]:
        """检测行业"""
        text_lower = text.lower()
        scores = {}
        for industry, keywords in self.INDUSTRY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            if score > 0:
                scores[industry] = score
        if scores:
            return max(scores, key=scores.get)
        return None
    
    def _extract_business(self, text: str) -> Optional[str]:
        """提取业务描述"""
        patterns = [
            r"主营业务[：:]\s*(.+?)(?:\n|$)",
            r"业务范围[：:]\s*(.+?)(?:\n|$)",
            r"经营范围[：:]\s*(.+?)(?:\n|$)",
            r"我们(主要)?提供(.+?)(?:[。\n]|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                result = match.group(0) if len(match.groups()) == 0 else match.group(len(match.groups()))
                return result.strip()[:200]
        return None
    
    def _extract_target_users(self, text: str) -> Optional[str]:
        """提取目标用户"""
        patterns = [
            r"目标(客户|用户|人群)[：:]\s*(.+?)(?:\n|$)",
            r"受众[：:]\s*(.+?)(?:\n|$)",
            r"服务对象[：:]\s*(.+?)(?:\n|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(match.lastindex).strip()[:100]
        return None
    
    def _extract_products(self, text: str) -> Optional[str]:
        """提取产品信息"""
        patterns = [
            r"产品[：:]\s*(.+?)(?:\n|$)",
            r"服务[：:]\s*(.+?)(?:\n|$)",
            r"核心产品[：:]\s*(.+?)(?:\n|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()[:200]
        return None
    
    def _extract_pain_points(self, text: str) -> Optional[str]:
        """提取用户痛点"""
        patterns = [
            r"痛点[：:]\s*(.+?)(?:\n|$)",
            r"问题[：:]\s*(.+?)(?:\n|$)",
            r"困扰[：:]\s*(.+?)(?:\n|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()[:200]
        return None
    
    def _extract_advantages(self, text: str) -> Optional[str]:
        """提取竞争优势"""
        patterns = [
            r"优势[：:]\s*(.+?)(?:\n|$)",
            r"特色[：:]\s*(.+?)(?:\n|$)",
            r"核心竞争力[：:]\s*(.+?)(?:\n|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()[:200]
        return None


# 单例
_analyzer_instance: Optional[FileAnalyzer] = None


def get_file_analyzer() -> FileAnalyzer:
    """获取文件分析器单例"""
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = FileAnalyzer()
    return _analyzer_instance


def analyze_file(file_path: str) -> Dict[str, Any]:
    """便捷函数：分析文件"""
    return get_file_analyzer().analyze_file(file_path)


def analyze_text(text: str) -> Dict[str, Any]:
    """便捷函数：分析文本"""
    return get_file_analyzer().analyze_text(text)
