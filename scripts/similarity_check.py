from difflib import SequenceMatcher
import re

original = """前段时间马斯克在X评论说AI将取代搜索，揭开了营销圈近期很火的话题，GEO正在取代SEO，还用SEO老办法抢AI时代的流量的品牌注定会被淘汰。以前咱们买东西啊先搜一搜，现在直接问AI我该买啥，前面搜索找答案的呢就是SEO，后面问AI直接给答案呢就是GEO。GEO市场增速你知道有多惊人吗？从二零一九年的七十二亿元一路飙升，二零二四年已经到了一百六十七亿元，反观我们的SEO市场却不断萎缩，到二零二八年可能只有四十一亿元，跌回了二零一九年一半的水平。品牌未来要打的是GEO之战，谁能成为AI的标准答案，谁就赢在未来。大家好，我是深耕中美营销十多年，把营销趋势最前沿带给行内人的Dorsey Doris。在人人焦虑的AI时代下，品牌到底如何用好AI，营销还能怎么做呢？今天我们先讲第一个营销人都要学的新模式GEO。GEO为什么会取代SEO呢？背后一句话就是。AI让消费者变懒了。"""

rewrite = """前段时间马斯克在X评论说AI将取代搜索，揭开了营销圈近期很火的话题：GEO正在取代SEO，还用SEO老办法抢AI时代流量的品牌，注定会被淘汰。以前咱们买东西先搜一搜，现在直接问AI"我该买啥"——前面搜索找答案的是SEO，后面问AI直接给答案的就是GEO。GEO市场增速你知道有多惊人吗？从2019年的72亿元一路飙升，到2024年已经到了167亿元；反观SEO市场却不断萎缩，到2028年可能只剩41亿元，跌回2019年一半的水平。品牌未来要打的，是GEO之战——谁能成为AI的标准答案，谁就赢在未来。大家好，我是专注GEO优化实战五年的流量周期守夜人。在人人焦虑的AI时代下，企业到底怎么活下来？今天我们先讲第一个所有老板都必须搞懂的新逻辑：GEO。GEO为什么会取代SEO？背后就一句话：AI让消费者变懒了。"""

# 整体字符相似度
overall = SequenceMatcher(None, original, rewrite).ratio()
print(f"=== 相似度分析 ===")
print(f"整体相似度: {overall*100:.1f}%")

# 分句对比
orig_sentences = [s.strip() for s in re.split(r'[。！？]', original) if s.strip()]
rewrite_sentences = [s.strip() for s in re.split(r'[。！？]', rewrite) if s.strip()]

print(f"\n原文句子数: {len(orig_sentences)}")
print(f"仿写句子数: {len(rewrite_sentences)}")

high_sim = []
for orig_s in orig_sentences:
    for rew_s in rewrite_sentences:
        sim = SequenceMatcher(None, orig_s, rew_s).ratio()
        if sim > 0.7:
            high_sim.append((sim, orig_s[:40], rew_s[:40]))
            break

print(f"\n高相似句子(>70%): {len(high_sim)}/{len(orig_sentences)}")
print("\n[高危句子详情]:")
for sim, orig, rew in sorted(high_sim, reverse=True)[:5]:
    print(f"  {sim*100:.0f}%: '{orig}...'")

# 洗稿风险评估
print("\n=== 洗稿风险评估 ===")
if overall > 0.8:
    print("❌ 高风险: 整体相似度过高，极可能被判定为洗稿")
elif overall > 0.6:
    print("⚠️ 中风险: 相似度较高，有洗稿嫌疑")
elif overall > 0.4:
    print("✅ 低风险: 相似度适中，属于合理借鉴范围")
else:
    print("✅ 安全: 相似度低，不太可能被判定为洗稿")
