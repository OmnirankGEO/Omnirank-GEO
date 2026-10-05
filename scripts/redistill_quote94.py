"""
重新蒸馏Quote 94的distilled_data
用法: cd geo_agentscope && python scripts/redistill_quote94.py
"""
import asyncio
import sys
import os
import io
import json
import sqlite3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 加载.env环境变量
from dotenv import load_dotenv
load_dotenv()

QUOTE_ID = 94
BRAND_ID = 17
DIAGNOSIS_ID = 107
DB_PATH = "db/geo_diagnosis.db"


async def main():
    # 1. 从诊断记录加载原始数据
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT raw_data_json, keywords FROM diagnosis_records WHERE id=?", (DIAGNOSIS_ID,)).fetchone()
    if not row or not row["raw_data_json"]:
        print("[ERROR] 找不到诊断记录107的raw_data_json")
        conn.close()
        return

    diagnosis_data = json.loads(row["raw_data_json"])
    print(f"[INFO] 已加载诊断数据: brand={diagnosis_data.get('brand')}, keys={list(diagnosis_data.keys())}")

    # 2. 运行蒸馏管道
    from writing.distiller import DistillerPipeline
    pipeline = DistillerPipeline(
        diagnosis_data=diagnosis_data,
        brand_id=BRAND_ID
    )
    print("[INFO] 开始蒸馏（使用 dashscope/qwen3-max）...")
    distilled = await pipeline.run()

    # 3. 检查结果
    for key in ["client_profile", "selling_points", "competitor_analysis"]:
        val = distilled.get(key, "")
        is_error = isinstance(val, str) and val.startswith("[LLM")
        status = "❌ 失败" if is_error else "✅ 成功"
        preview = str(val)[:200] if val else "(空)"
        print(f"  {status} {key}: {preview}")

    # 4. 保存到quotes表
    distilled_json = json.dumps(distilled, ensure_ascii=False)
    conn.execute("UPDATE quotes SET distilled_data = ? WHERE id = ?", (distilled_json, QUOTE_ID))
    conn.commit()
    print(f"\n[SUCCESS] 已更新 quotes.distilled_data (quote_id={QUOTE_ID})")

    # 5. 同时保存到diagnosis_records
    conn.execute("UPDATE diagnosis_records SET distilled_data = ? WHERE id = ?", (distilled_json, DIAGNOSIS_ID))
    conn.commit()
    print(f"[SUCCESS] 已更新 diagnosis_records.distilled_data (id={DIAGNOSIS_ID})")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
