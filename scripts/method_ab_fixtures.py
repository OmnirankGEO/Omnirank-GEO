"""30 场景 fixture · 给 method-selection A/B 压测用

覆盖矩阵:
  - 8 行业(餐饮/教培/房产/医美/法律/金融/留学/装修)
  - 5 客户问题类型(决策犹豫/比对纠结/价格敏感/信任不足/选错恐惧)
  - 4 写手 advisor(xuehui 富 / xingyi 富 / cange-team 中 / huang-douyin 极弱)
  - 5 极端场景(模糊 / 跨多 method / 行业敏感)

每个 fixture 含:
  - id, industry, advisor_id (写手)
  - customer_scenario (text · 模拟代理输入)
  - profile_snapshot (7 字段)
  - expected_signal (hint · 不强制 judge)
  - expected_content_type (judge 验类型)
  - notes (设计理由)

注:这是 mock fixture · 用于 baseline 评分。
后续可以从 prod social_topics / content_plans 表抽真实 case 替换。
"""

from __future__ import annotations

FIXTURES: list[dict] = [
    # ============================================================
    # 餐饮 · 3 场景
    # ============================================================
    {
        "id": "fix-001-food-decision",
        "industry": "餐饮",
        "advisor_id": "xuehui",
        "customer_scenario": "我有一个开海鲜大排档的客户 · 30 桌规模 · 老板纠结要不要做加盟 · 怕加盟商把品牌做烂 · 但又想快速扩张 · 帮我写一条说他这种心态的视频",
        "profile_snapshot": {
            "industry": "餐饮 · 海鲜大排档",
            "business": "本地海鲜餐厅 · 开发加盟模式",
            "target_users": "想做餐饮加盟的小老板 / 年龄 30-45 / 有 50-100 万启动资金",
            "selling_points": "海鲜直供 / 总店每年 800 万营业额 / 老板亲自带店",
            "pain_points": "加盟商管不住 / 品控滑坡 / 总店利润被分摊",
            "success_cases": "去年 1 个加盟店第 3 个月就破 80 万月营业",
            "testimonials": "",
        },
        "expected_signal": ["创业期", "B端"],
        "expected_content_type": "人设型",
        "notes": "决策犹豫型 · 老板视角",
    },
    {
        "id": "fix-002-food-price",
        "industry": "餐饮",
        "advisor_id": "xingyi",
        "customer_scenario": "客户开火锅店 · 想用短视频拉新 · 老板说同行打 5 折他不想跟着卷 · 但流量又上不来 · 写一条不打价格战的吸客视频",
        "profile_snapshot": {
            "industry": "餐饮 · 重庆老火锅",
            "business": "60 桌火锅店 · 主打牛油老灶",
            "target_users": "本地火锅常客 / 25-45 岁 / 朋友聚会场景",
            "selling_points": "祖传牛油配方 / 老灶现熬 / 食材当天到店",
            "pain_points": "同行打折太狠 / 老板不想跟卷 / 流量焦虑",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["价格敏感", "成交转化"],
        "expected_content_type": "观点型",
        "notes": "价格敏感 · 立场型钩子",
    },
    {
        "id": "fix-003-food-trust",
        "industry": "餐饮",
        "advisor_id": "cange-team",
        "customer_scenario": "我做轻食代餐品牌的客户 · 推新品 · 想用一条视频说服客户不要信网红同款 · 写一条建立信任的内容",
        "profile_snapshot": {
            "industry": "餐饮 · 轻食代餐",
            "business": "轻食代餐 DTC 品牌 · 自营工厂",
            "target_users": "25-35 岁白领 · 健身减脂人群 / C 端",
            "selling_points": "自营工厂 / 营养师配比 / 7 天极速供应链",
            "pain_points": "市场充斥假冒网红同款 / 客户被坑后不信",
            "success_cases": "复购率 42%",
            "testimonials": "用户晒图说'吃完没虚饥感'",
        },
        "expected_signal": ["信任不足", "健康教育"],
        "expected_content_type": "变现型",
        "notes": "信任建立 · 反 网红同款",
    },

    # ============================================================
    # 教培 · 3 场景
    # ============================================================
    {
        "id": "fix-004-edu-comparison",
        "industry": "教培",
        "advisor_id": "xuehui",
        "customer_scenario": "我有一个做高中 1 对 1 数学辅导的客户 · 家长爱比价 · 怕被坑 · 家长经常问'你跟 XX 机构差啥' · 写一条帮老板表达他课怎么不一样的内容",
        "profile_snapshot": {
            "industry": "教培 · K12 数学 1 对 1",
            "business": "高中数学 1 对 1 · 主打高三冲刺",
            "target_users": "高三家长 + 学生 / 2-3 个月内提分 30+ 需求",
            "selling_points": "原班级前 10% 学生 80% 提分 30+ / 老师都是省重点教龄 10 年+",
            "pain_points": "家长比价 / 怕被坑 / 不懂教学差异",
            "success_cases": "去年高三 18 个学生 14 个 985 / 4 个 211",
            "testimonials": "家长说'孩子从 90 分到 130 分'",
        },
        "expected_signal": ["比对纠结", "家长"],
        "expected_content_type": "变现型",
        "notes": "比对纠结 · 家长视角",
    },
    {
        "id": "fix-005-edu-fear",
        "industry": "教培",
        "advisor_id": "xingyi",
        "customer_scenario": "做编程培训的客户 · 家长担心 IT 行业未来不好 · 担心孩子学完没用 · 写一条缓解家长焦虑的内容",
        "profile_snapshot": {
            "industry": "教培 · 少儿编程",
            "business": "9-15 岁少儿编程 · Scratch + Python",
            "target_users": "小学高年级到初中家长 / 关心孩子升学 + 未来竞争力",
            "selling_points": "对接信奥 / 全国白名单赛事 / 升学加分",
            "pain_points": "家长 IT 行业焦虑 / 怕浪费时间 / 怕孩子没兴趣",
            "success_cases": "去年 12 个学员进省队 / 6 个拿一等奖",
            "testimonials": "",
        },
        "expected_signal": ["选错恐惧", "家长", "教育"],
        "expected_content_type": "人设型",
        "notes": "选错恐惧 · 家长焦虑",
    },
    {
        "id": "fix-006-edu-traffic",
        "industry": "教培",
        "advisor_id": "xingyi",
        "customer_scenario": "客户开雅思培训 · 账号刚起号 · 没流量 · 不知道发什么 · 写一条拉新爆款选题视频",
        "profile_snapshot": {
            "industry": "教培 · 雅思",
            "business": "雅思 6-7 分冲刺培训",
            "target_users": "大三大四学生 / 工作党留学预备",
            "selling_points": "中考雅思双 8 老师 / 全程小班 / 模考次数多",
            "pain_points": "起号没流量 / 选题没方向",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["起号定位", "短视频运营"],
        "expected_content_type": "流量型",
        "notes": "起号没方向 · 流量型钩子",
    },

    # ============================================================
    # 房产 · 4 场景
    # ============================================================
    {
        "id": "fix-007-house-decision",
        "industry": "房产",
        "advisor_id": "xuehui",
        "customer_scenario": "我有一个在揭阳本地做商铺销售的客户 · 客户群是当地做生意的老板 · 看了 3 次房都没下单 · 怕选错位置 · 写一条说服他这种生意人选商铺的视频",
        "profile_snapshot": {
            "industry": "房产 · 商铺销售",
            "business": "揭阳市区临街商铺销售 · 30-80 万一间",
            "target_users": "揭阳本地做生意的老板 / 想做资产配置 / 35-55 岁",
            "selling_points": "本地学区配套 / 街角铺位 / 出租回报年化 6%",
            "pain_points": "客户怕选错位置 / 看 3 次不下单 / 怕亏",
            "success_cases": "去年王总买 2 楼街角 · 半年租出 · 月租 1.2 万",
            "testimonials": "王总说'我以为商铺都是接盘 · 这间没想到能租出'",
        },
        "expected_signal": ["决策犹豫", "B端", "投资理财"],
        "expected_content_type": "变现型",
        "notes": "老板报告 boss 原始 case · 揭阳生意人买商铺",
    },
    {
        "id": "fix-008-house-price",
        "industry": "房产",
        "advisor_id": "cange-team",
        "customer_scenario": "做高端别墅销售的客户 · 客户嫌贵但又心动 · 怕买完后悔 · 写一条让他相信值这个价的视频",
        "profile_snapshot": {
            "industry": "房产 · 高端别墅",
            "business": "杭州西湖区高端别墅 · 单价 8-15 万 / 平",
            "target_users": "高净值人群 / 资产 5000 万+ / 改善型",
            "selling_points": "学区 + 西湖景观 / 限量产品 / 抗跌性强",
            "pain_points": "嫌贵 / 怕买完跌 / 心动但不敢下手",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["价格敏感", "选错恐惧"],
        "expected_content_type": "观点型",
        "notes": "价格敏感 · 高净值人群心智",
    },
    {
        "id": "fix-009-house-comparison",
        "industry": "房产",
        "advisor_id": "xuehui",
        "customer_scenario": "我有一个做民宿合伙投资的客户 · 客户在比对 3 个投资方案 · 一直定不下来 · 写一条帮他出决策的视频",
        "profile_snapshot": {
            "industry": "房产 · 民宿合伙投资",
            "business": "莫干山 / 大理 / 黄山 民宿合伙投资",
            "target_users": "想做被动收入的中产 / 资产 200-500 万",
            "selling_points": "包租回报 6-8% / 入驻分红 / 退出灵活",
            "pain_points": "比对纠结 / 怕踩坑 / 信息不对称",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["比对纠结"],
        "expected_content_type": "干货型",
        "notes": "比对纠结 · 帮决策",
    },
    {
        "id": "fix-010-house-trust",
        "industry": "房产",
        "advisor_id": "cange-team",
        "customer_scenario": "做老破小翻新代购的客户 · 业主担心被骗 · 总怀疑价格不透明 · 写一条建立信任的视频",
        "profile_snapshot": {
            "industry": "房产 · 二手房代购 + 翻新",
            "business": "一线城市 老破小代购 + 翻新一站式",
            "target_users": "刚需自住 / 预算 200-400 万 / 怕装修被坑",
            "selling_points": "全程透明 / 装修不加价 / 房源筛选 200 套挑 5 套",
            "pain_points": "客户怕被坑 / 怕价格虚高 / 怕装修猫腻",
            "success_cases": "去年 11 户全部按签约预算交付",
            "testimonials": "",
        },
        "expected_signal": ["信任不足", "家装"],
        "expected_content_type": "变现型",
        "notes": "信任建立",
    },

    # ============================================================
    # 医美 · 3 场景
    # ============================================================
    {
        "id": "fix-011-beauty-decision",
        "industry": "医美",
        "advisor_id": "xuehui",
        "customer_scenario": "我有一个做面部精雕的客户 · 求美者咨询了 2 次 · 一直定不下来 · 怕脸打废 · 写一条帮她下决心的视频",
        "profile_snapshot": {
            "industry": "医美 · 面部抗衰精雕",
            "business": "高端医美 · 面部精雕 · 单次 3-8 万",
            "target_users": "30-45 岁女性 / 经济独立 / 有审美追求",
            "selling_points": "10 年面部医生 / 私人方案设计 / 术后跟踪 90 天",
            "pain_points": "怕打废 / 怕风险 / 怕浪费钱",
            "success_cases": "去年 86 例零事故 / 客户满意度 95%",
            "testimonials": "客户说'医生给我的方案是越自然越年轻'",
        },
        "expected_signal": ["选错恐惧", "求美者", "医美咨询"],
        "expected_content_type": "变现型",
        "notes": "决策犹豫 · 求美者心智",
    },
    {
        "id": "fix-012-beauty-fear",
        "industry": "医美",
        "advisor_id": "huang-douyin",
        "customer_scenario": "做轻医美项目的客户 · 老板讲心智 · 让她重新认识自己 · 写一条情绪型视频",
        "profile_snapshot": {
            "industry": "医美 · 轻医美",
            "business": "轻医美项目 · 玻尿酸 + 水光 + 抗衰",
            "target_users": "25-40 岁女性 / 想美但怕被针扎",
            "selling_points": "无痛技术 / 1 次见效 / 院长亲做",
            "pain_points": "怕痛 / 怕没效果 / 怕被坑",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["选错恐惧", "求美者"],
        "expected_content_type": "故事型",
        "notes": "情绪共鸣 + huang-douyin 数据极弱 · 看 A/B 是否能 carry",
    },
    {
        "id": "fix-013-beauty-traffic",
        "industry": "医美",
        "advisor_id": "xingyi",
        "customer_scenario": "客户做医美修复 · 账号没起号 · 想用反常识切入 · 写一条破圈拉新的视频",
        "profile_snapshot": {
            "industry": "医美 · 修复",
            "business": "医美失败修复 · 二次修复",
            "target_users": "医美失败受害者 / 想修复 / 30-50 岁",
            "selling_points": "10 年修复经验 / 难度大的案例多 / 院长亲做",
            "pain_points": "受众小众 / 起号没流量",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["起号定位", "医美行业"],
        "expected_content_type": "流量型",
        "notes": "破圈拉新 · 反常识钩子",
    },

    # ============================================================
    # 法律 · 3 场景
    # ============================================================
    {
        "id": "fix-014-legal-fear",
        "industry": "法律",
        "advisor_id": "xuehui",
        "customer_scenario": "做离婚财产律师的客户 · 客户咨询时纠结要不要离 · 怕分错钱 · 写一条她视角的视频",
        "profile_snapshot": {
            "industry": "法律 · 婚姻家事",
            "business": "婚姻家事律师 · 离婚财产 + 子女抚养",
            "target_users": "30-50 岁离婚预备人群 / 主要女性",
            "selling_points": "20 年专做婚家 / 上海 top 100 律师 / 大额财产经验",
            "pain_points": "客户怕分错钱 / 怕孩子归属 / 决策焦虑",
            "success_cases": "去年 38 例平均争取 + 2-3 倍财产",
            "testimonials": "",
        },
        "expected_signal": ["决策犹豫", "婚姻家事", "继承离婚"],
        "expected_content_type": "故事型",
        "notes": "情绪 + 法律视角",
    },
    {
        "id": "fix-015-legal-trust",
        "industry": "法律",
        "advisor_id": "cange-team",
        "customer_scenario": "做劳动法工伤律师的客户 · 客户被公司压赔偿 · 不敢起诉 · 怕得罪老板找不到工作 · 写一条建立信任的视频",
        "profile_snapshot": {
            "industry": "法律 · 劳动法",
            "business": "工伤劳动纠纷律师",
            "target_users": "工伤员工 / 25-50 岁 / 蓝领多",
            "selling_points": "工伤认定经验 200+ 案 / 不胜诉不收费",
            "pain_points": "客户怕得罪老板 / 怕事情更大 / 怕拿不到钱",
            "success_cases": "去年帮 35 个工伤员工拿回 平均 20 万赔偿",
            "testimonials": "客户说'律师帮我打,公司一周内就赔了'",
        },
        "expected_signal": ["信任不足", "劳动纠纷当事人", "劳动者"],
        "expected_content_type": "变现型",
        "notes": "信任建立 + 劳动法",
    },
    {
        "id": "fix-016-legal-opinion",
        "industry": "法律",
        "advisor_id": "xuehui",
        "customer_scenario": "做企业合规法律的客户 · 想对 老板们说一个观点 · '签合同前不看这条 · 后面打官司输 80%' · 写一条观点视频",
        "profile_snapshot": {
            "industry": "法律 · 企业合规",
            "business": "中小企业合规律师 · 合同审查 + 法务外包",
            "target_users": "中小企业老板 / 30-55 岁",
            "selling_points": "20 年企业合规 / 服务过 200+ 客户 / 上市公司法务背景",
            "pain_points": "老板不重视合规 / 出事才找律师",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["B端", "创始人", "法律咨询"],
        "expected_content_type": "观点型",
        "notes": "立场型 · 老板视角",
    },

    # ============================================================
    # 金融(基金/期货)· 3 场景
    # ============================================================
    {
        "id": "fix-017-finance-decision",
        "industry": "金融",
        "advisor_id": "xuehui",
        "customer_scenario": "做基金配置咨询的客户 · 客户在比 5 个基金 · 选不出来 · 怕选错 · 写一条帮他建立信心的视频",
        "profile_snapshot": {
            "industry": "金融 · 基金配置",
            "business": "公募 + 私募基金配置咨询",
            "target_users": "高净值人群 / 资产 500 万+ / 30-55 岁",
            "selling_points": "10 年公募研究员 / 每年帮 100+ 客户配置",
            "pain_points": "客户比对选不出 / 怕亏 / 信息过载",
            "success_cases": "去年配置组合年化 12% · 远超同类平均",
            "testimonials": "",
        },
        "expected_signal": ["比对纠结", "投资理财", "基金配置"],
        "expected_content_type": "干货型",
        "notes": "比对选不出 · 帮决策",
    },
    {
        "id": "fix-018-finance-opinion",
        "industry": "金融",
        "advisor_id": "xingyi",
        "customer_scenario": "做期货交易心理咨询的客户 · 老板讲一个反常识观点 · '亏钱的不是技术 · 是情绪' · 写一条立场型视频",
        "profile_snapshot": {
            "industry": "金融 · 期货心理",
            "business": "期货交易心理 + 复盘咨询",
            "target_users": "期货交易者 / 30-50 岁 / 主要男性",
            "selling_points": "20 年期货 / 个人交易盈利 + 公司交易 双经验",
            "pain_points": "客户重技术不重心态 / 反复亏",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["A股", "普通投资者"],
        "expected_content_type": "观点型",
        "notes": "反常识立场",
    },
    {
        "id": "fix-019-finance-fear",
        "industry": "金融",
        "advisor_id": "cange-team",
        "customer_scenario": "做家族信托的客户 · 客户怕子女拿到钱挥霍 · 但又不放心放在银行 · 写一条说服他做家族信托的视频",
        "profile_snapshot": {
            "industry": "金融 · 家族信托",
            "business": "家族信托设立 + 财富传承",
            "target_users": "高净值 / 资产 1 亿+ / 45-65 岁",
            "selling_points": "顶级机构合作 / 多代传承设计 / 全流程合规",
            "pain_points": "怕子女挥霍 / 怕政策变 / 不懂信托",
            "success_cases": "服务过 30 个亿万家庭",
            "testimonials": "",
        },
        "expected_signal": ["选错恐惧", "继承离婚"],
        "expected_content_type": "故事型",
        "notes": "情绪故事 + 高净值",
    },

    # ============================================================
    # 留学 · 3 场景
    # ============================================================
    {
        "id": "fix-020-study-decision",
        "industry": "留学",
        "advisor_id": "xuehui",
        "customer_scenario": "做美国留学申请的客户 · 家长在公立私立之间纠结 · 一直定不下来 · 写一条帮她决策的视频",
        "profile_snapshot": {
            "industry": "留学 · 美国本科",
            "business": "美国本科留学申请 · 全程服务",
            "target_users": "高一到高三学生家长 / 中产家庭",
            "selling_points": "10 年美本申请 / 平均录取率 90%+ top50",
            "pain_points": "家长比对纠结 / 怕选错 / 信息过载",
            "success_cases": "去年 18 个学员 12 个 top30 录取",
            "testimonials": "",
        },
        "expected_signal": ["美国留学", "家长", "申请季"],
        "expected_content_type": "干货型",
        "notes": "决策犹豫 + 家长视角",
    },
    {
        "id": "fix-021-study-fear",
        "industry": "留学",
        "advisor_id": "xingyi",
        "customer_scenario": "做加拿大移民的客户 · 客户怕政策变 · 怕花钱白搭 · 写一条缓解焦虑的视频",
        "profile_snapshot": {
            "industry": "留学 · 加拿大移民",
            "business": "加拿大留学 + 移民一站式",
            "target_users": "想移民加拿大的 25-40 岁 / 家庭 50-200 万预算",
            "selling_points": "10 年加拿大移民 / 100% 合规通道 / 全程跟踪",
            "pain_points": "怕政策变 / 怕花钱白搭",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["选错恐惧", "加拿大移民"],
        "expected_content_type": "人设型",
        "notes": "信任建立 + 移民",
    },
    {
        "id": "fix-022-study-comparison",
        "industry": "留学",
        "advisor_id": "cange-team",
        "customer_scenario": "做香港留学的客户 · 客户拿到了 3 个 offer 选不出 · 写一条帮她选 offer 的视频",
        "profile_snapshot": {
            "industry": "留学 · 香港硕士",
            "business": "香港硕士申请 + 求职辅导",
            "target_users": "大四学生 / 想留香港工作或回内地",
            "selling_points": "港大港中文港科大 3 校全 alumni · offer 选择经验",
            "pain_points": "比对纠结 / offer 选错 / 怕影响就业",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["比对纠结", "香港留学"],
        "expected_content_type": "干货型",
        "notes": "比对纠结 · 帮选 offer",
    },

    # ============================================================
    # 装修 · 3 场景
    # ============================================================
    {
        "id": "fix-023-deco-fear",
        "industry": "装修",
        "advisor_id": "xuehui",
        "customer_scenario": "做装修公司的客户 · 业主怕被装修公司套路 · 怕增项 · 怕材料偷工 · 写一条建立信任的视频",
        "profile_snapshot": {
            "industry": "装修 · 全包",
            "business": "二三线城市全包装修 · 100-300 平 单",
            "target_users": "30-45 岁刚需自住 / 预算 30-80 万",
            "selling_points": "签前透明报价 / 不增项承诺 / 材料明细公开",
            "pain_points": "客户怕被坑 / 怕增项 / 怕材料假",
            "success_cases": "去年 200 户全部按签约预算交付",
            "testimonials": "客户说'装完没多花一分钱'",
        },
        "expected_signal": ["信任不足", "装修", "家装"],
        "expected_content_type": "变现型",
        "notes": "信任建立 · 装修最常见",
    },
    {
        "id": "fix-024-deco-tutorial",
        "industry": "装修",
        "advisor_id": "huang-douyin",
        "customer_scenario": "做装修砍价的客户 · 业主想学怎么砍报价 · 写一条教业主砍价的视频",
        "profile_snapshot": {
            "industry": "装修 · 砍价代理",
            "business": "装修砍价代理 + 报价审计",
            "target_users": "装修业主 / 30-45 岁 / 不懂装修",
            "selling_points": "10 年砍价经验 / 平均帮砍 20-30%",
            "pain_points": "客户不会砍价 / 怕被坑",
            "success_cases": "去年 50 户平均砍下来 8 万",
            "testimonials": "",
        },
        "expected_signal": ["装修前期", "选购期"],
        "expected_content_type": "干货型",
        "notes": "干货 + huang-douyin 数据极弱(测低数据 advisor 表现)",
    },
    {
        "id": "fix-025-deco-traffic",
        "industry": "装修",
        "advisor_id": "xingyi",
        "customer_scenario": "做设计师 IP 的客户 · 想破圈拉新 · 用一个反常识钩子 · 写一条流量型视频",
        "profile_snapshot": {
            "industry": "装修 · 设计师 IP",
            "business": "设计师个人 IP + 高端别墅设计",
            "target_users": "高净值业主 / 想做独立设计",
            "selling_points": "10 年别墅设计 / 上过电视设计节目",
            "pain_points": "没流量 / 同行同质化",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["短视频运营", "起号定位"],
        "expected_content_type": "流量型",
        "notes": "破圈钩子",
    },

    # ============================================================
    # 极端场景 · 5 个
    # ============================================================
    {
        "id": "fix-026-vague",
        "industry": "未指定",
        "advisor_id": "xuehui",
        "customer_scenario": "我想写个内容",
        "profile_snapshot": {
            "industry": "未指定",
            "business": "",
            "target_users": "",
            "selling_points": "",
            "pain_points": "",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": [],
        "expected_content_type": "干货型",
        "notes": "极端模糊 · L1 应 confidence < 0.3 · L3 应退老 prompt",
    },
    {
        "id": "fix-027-multi-method",
        "industry": "餐饮",
        "advisor_id": "xuehui",
        "customer_scenario": "我有一个轻食店客户 · 既想用视频拉新精准流量 · 又想转化老客户加群 · 还想建立人设 · 写一条多目标视频",
        "profile_snapshot": {
            "industry": "餐饮 · 轻食",
            "business": "轻食店 + 私域社群",
            "target_users": "25-35 岁白领",
            "selling_points": "自营工厂 + 私域分享群",
            "pain_points": "多目标想一起做",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["短视频运营", "私域增长"],
        "expected_content_type": "人设型",
        "notes": "跨多 method · 应选 primary",
    },
    {
        "id": "fix-028-sensitive-medical",
        "industry": "口腔",
        "advisor_id": "xuehui",
        "customer_scenario": "做口腔诊所的客户 · 想用视频告诉家长 '孩子换牙期不矫正后果严重' · 帮他写一条让家长来店的视频",
        "profile_snapshot": {
            "industry": "口腔 · 儿牙",
            "business": "儿童口腔诊所 · 矫正 + 检查",
            "target_users": "6-12 岁孩子家长",
            "selling_points": "儿牙专科 / 经验 10 年+",
            "pain_points": "家长不重视换牙期 / 怕治疗痛",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["家长", "医学科普", "健康教育"],
        "expected_content_type": "人设型",
        "notes": "敏感行业(口腔)· 测 96 分硬规则 / 医疗导向话术拦截",
    },
    {
        "id": "fix-029-low-data-advisor",
        "industry": "金融",
        "advisor_id": "huang-douyin",
        "customer_scenario": "做股票培训的客户 · 想用一条视频说服客户跟单 · 写一条变现视频",
        "profile_snapshot": {
            "industry": "金融 · 股票培训",
            "business": "股票交易培训",
            "target_users": "想做股票投资的 30-50 岁",
            "selling_points": "10 年实盘 · 培训过 300+ 人",
            "pain_points": "客户怕被坑 / 怕割韭菜",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["A股", "普通投资者"],
        "expected_content_type": "变现型",
        "notes": "huang-douyin 数据极弱(4 chunks)+ 金融敏感 · 测 worst case",
    },
    {
        "id": "fix-030-conflicting-pain",
        "industry": "美业",
        "advisor_id": "cange-team",
        "customer_scenario": "做美甲店的客户 · 客户群是 20 多岁女生 · 但她们既想要便宜 · 又想要好看款式 · 还想要快出门 · 写一条满足这种心态的视频",
        "profile_snapshot": {
            "industry": "美业 · 美甲",
            "business": "美甲店 · 主打学生白领",
            "target_users": "20-30 岁女性 / 学生 + 白领",
            "selling_points": "30 分钟出门 / 100-200 价位 / 流行款多",
            "pain_points": "客户想便宜又想好看又想快",
            "success_cases": "",
            "testimonials": "",
        },
        "expected_signal": ["护肤", "C端用户"],
        "expected_content_type": "人设型",
        "notes": "多重矛盾痛点 · 测 method 选择难度",
    },
]


def get_fixtures_by_advisor(advisor_id: str) -> list[dict]:
    return [f for f in FIXTURES if f.get("advisor_id") == advisor_id]


def get_fixtures_by_industry(industry: str) -> list[dict]:
    return [f for f in FIXTURES if f.get("industry") == industry]


def fixture_summary() -> dict:
    from collections import Counter
    return {
        "total": len(FIXTURES),
        "by_advisor": dict(Counter(f.get("advisor_id", "?") for f in FIXTURES)),
        "by_industry": dict(Counter(f.get("industry", "?") for f in FIXTURES)),
        "by_expected_type": dict(Counter(f.get("expected_content_type", "?") for f in FIXTURES)),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(fixture_summary(), ensure_ascii=False, indent=2))
