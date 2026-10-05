"""
知识库蒸馏测试 — 用新版 prompt 蒸馏星壹课程，验证输出质量
测试 3 个文件：短文件(001) + 长文件(013-标签) + 规则类(019-审核)
"""
import os
import sys
import json
import asyncio
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

KNOWLEDGE_DIR = ROOT / "docs" / "知识库" / "星壹"
OUTPUT_DIR = ROOT / "data" / "distill_test"
ADVISOR_ID = "xinyi-formula"

# 测试文件
TEST_FILES = [
    "001-什么短视频公式？为什么要用公式来做短视频？.txt",
    "013-2.7怎么打标签？打标签公式新课.txt",
    "019-3.1短视频审核的规则？敏感词和违规词？辨别新课.txt",
]

DISTILL_PROMPT = """你是知识库蒸馏专家。将课程/录音转写内容提取为结构化知识块。

## 输入
顾问ID：{advisor_id}
章节标题：{title}
来源文件：{source_file}
课程原文（ASR转写）：
---
{content}
---

## 输出要求
从原文中提取独立的知识块。每个知识块只讲一个方法/一个观点/一个案例。
输出 JSON 数组，每个元素格式如下：

[
  {{
    "chunk_id": "{advisor_id}_{{序号}}",
    "advisor_id": "{advisor_id}",
    "title": "知识点标题（15-20字，名词短语，如'标签打法三层模型'）",
    "content": "蒸馏后的知识内容（300-800字）",
    "content_type": "methodology|case|rule|trend|framework",
    "applicable_to": ["适用阶段", "适用行业", "适用内容类型"],
    "keywords": ["检索关键词1", "关键词2", "关键词3"],
    "use_when": "什么场景下应该检索到这条知识",
    "dont_use_when": "不适用的场景（没有就留空字符串）",
    "related_concepts": ["关联的其他知识点名称"],
    "golden_quotes": ["值得保留的原话金句"],
    "source_file": "{source_file}",
    "source_section": "大致在原文的哪个位置"
  }}
]

## 蒸馏原则

### 什么该保留
- 具体的方法/公式/步骤（这是知识的核心价值）
- 真实的案例和数字（"美容院老板流量翻了5倍"）
- 老师的独到观点和洞察（这是区分度）
- 反直觉的结论（"起号期不要投付费流量"）
- 原话中特别精彩的表达（放到 golden_quotes）

### 什么该删掉
- 口语填充词："啊""对吧""其实原因非常简单""听到这"
- 课程过渡语："我们继续下一个""这节课讲的是"
- 自我推销："我的课程值10万""付费的同学应该会听下去吧"
- 重复内容：同一个知识点说了两遍的，合并成一条

### 关键要求
1. **一个知识块 = 一个独立的知识单元**，不要把"算法逻辑"和"标签打法"混在一块里
2. **title 要像搜索关键词**，用户搜"怎么打标签"时能匹配到"标签打法三层模型"
3. **applicable_to 很重要** — 决定了检索时能不能精准匹配。"起号期"和"成熟期"适用的知识完全不同
4. **content 保留老师的人格** — 去掉废话但保留说话风格。蒸馏不是写百科
5. 如果一段原文只是闲聊没有知识价值，直接跳过，不生成知识块
6. 单个文件预期提取 2-8 个知识块（不多不少）

只输出 JSON 数组，不要其他文字。"""


def call_llm_sync(prompt: str, model: str = "qwen3-max") -> str:
    """同步调用 DashScope"""
    import dashscope
    from dashscope import Generation

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")

    response = Generation.call(
        api_key=api_key,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        result_format="message",
        max_tokens=8000,
        temperature=0.3,
    )

    if response.status_code != 200:
        raise RuntimeError(f"DashScope 调用失败: {response.code} {response.message}")

    text = response.output.choices[0].message.content

    # 去除 think 标签
    import re
    if "<think>" in text:
        text = re.sub(r'<think>[\s\S]*?</think>', '', text).strip()

    return text


def extract_json(text: str) -> list:
    """从 LLM 输出中提取 JSON 数组"""
    import re
    # 尝试提取 ```json ... ``` 块
    match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if match:
        text = match.group(1)

    text = text.strip()

    try:
        from json_repair import repair_json
        result = json.loads(repair_json(text))
    except Exception:
        result = json.loads(text)

    if isinstance(result, dict):
        result = [result]
    return result


def distill_file(filepath: Path, advisor_id: str, chunk_offset: int = 0) -> list:
    """蒸馏单个文件"""
    content = filepath.read_text(encoding="utf-8")
    filename = filepath.name
    title = filepath.stem

    # 长文件分段（>6000字分两段）
    segments = []
    if len(content) > 6000:
        mid = len(content) // 2
        # 在中间找一个换行点
        split_point = content.rfind('\n', mid - 500, mid + 500)
        if split_point == -1:
            split_point = mid
        segments = [content[:split_point], content[split_point:]]
        print(f"  文件过长({len(content)}字)，分 {len(segments)} 段处理")
    else:
        segments = [content]

    all_chunks = []
    for seg_idx, segment in enumerate(segments):
        prompt = DISTILL_PROMPT.format(
            advisor_id=advisor_id,
            title=title,
            source_file=filename,
            content=segment[:8000],  # 安全截断
        )

        print(f"  段 {seg_idx+1}/{len(segments)}: {len(segment)}字 → LLM...")
        response = call_llm_sync(prompt)

        try:
            chunks = extract_json(response)
            # 修正 chunk_id
            for i, chunk in enumerate(chunks):
                chunk["chunk_id"] = f"{advisor_id}_{chunk_offset + len(all_chunks) + i + 1:03d}"
            all_chunks.extend(chunks)
            print(f"  → 提取 {len(chunks)} 个知识块")
        except Exception as e:
            print(f"  ⚠️ JSON 解析失败: {e}")
            print(f"  原始响应前 500 字: {response[:500]}")

    return all_chunks


def validate_chunks(chunks: list) -> dict:
    """验证蒸馏质量"""
    issues = []
    stats = {
        "total": len(chunks),
        "types": {},
        "avg_content_len": 0,
        "with_keywords": 0,
        "with_applicable": 0,
        "with_golden_quotes": 0,
    }

    total_len = 0
    for i, c in enumerate(chunks):
        # 必填字段检查
        for field in ["title", "content", "content_type", "keywords", "applicable_to"]:
            if not c.get(field):
                issues.append(f"chunk {i}: 缺少 {field}")

        # 内容长度检查
        content_len = len(c.get("content", ""))
        total_len += content_len
        if content_len < 100:
            issues.append(f"chunk {i} '{c.get('title', '?')}': content 太短({content_len}字)")
        if content_len > 1200:
            issues.append(f"chunk {i} '{c.get('title', '?')}': content 太长({content_len}字)")

        # 统计
        ct = c.get("content_type", "unknown")
        stats["types"][ct] = stats["types"].get(ct, 0) + 1
        if c.get("keywords"):
            stats["with_keywords"] += 1
        if c.get("applicable_to"):
            stats["with_applicable"] += 1
        if c.get("golden_quotes"):
            stats["with_golden_quotes"] += 1

    stats["avg_content_len"] = total_len // max(len(chunks), 1)
    stats["issues"] = issues
    return stats


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_chunks = []
    chunk_offset = 0

    for filename in TEST_FILES:
        filepath = KNOWLEDGE_DIR / filename
        if not filepath.exists():
            print(f"⚠️ 文件不存在: {filepath}")
            continue

        print(f"\n{'='*60}")
        print(f"蒸馏: {filename} ({filepath.stat().st_size / 1024:.1f}KB)")
        print(f"{'='*60}")

        chunks = distill_file(filepath, ADVISOR_ID, chunk_offset)
        chunk_offset += len(chunks)
        all_chunks.extend(chunks)

    # 保存结果
    output_file = OUTPUT_DIR / f"{ADVISOR_ID}_test.jsonl"
    with open(output_file, "w", encoding="utf-8") as f:
        for chunk in all_chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    print(f"\n{'='*60}")
    print(f"蒸馏完成: {len(all_chunks)} 个知识块 → {output_file}")
    print(f"{'='*60}")

    # 质量验证
    stats = validate_chunks(all_chunks)
    print(f"\n📊 质量报告:")
    print(f"  总知识块: {stats['total']}")
    print(f"  类型分布: {stats['types']}")
    print(f"  平均内容长度: {stats['avg_content_len']} 字")
    print(f"  有关键词: {stats['with_keywords']}/{stats['total']}")
    print(f"  有适用场景: {stats['with_applicable']}/{stats['total']}")
    print(f"  有金句: {stats['with_golden_quotes']}/{stats['total']}")
    if stats["issues"]:
        print(f"\n  ⚠️ 问题 ({len(stats['issues'])} 个):")
        for issue in stats["issues"]:
            print(f"    - {issue}")
    else:
        print(f"\n  ✅ 无质量问题")

    # 打印前 2 个知识块样例
    print(f"\n{'='*60}")
    print(f"样例输出（前 2 个知识块）:")
    print(f"{'='*60}")
    for chunk in all_chunks[:2]:
        print(json.dumps(chunk, ensure_ascii=False, indent=2))
        print("---")


if __name__ == "__main__":
    main()
