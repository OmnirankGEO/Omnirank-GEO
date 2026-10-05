-- 创建星壹 advisor 记录（如果不存在）
-- advisor_id 必须是 "星壹"，和 distilled_knowledge.json 里的 advisor_id 一致

INSERT INTO advisors (id, name, description, avatar, api_provider, model_name, is_active, role, specialty)
VALUES (
    '星壹',
    '星壹',
    '公式化短视频运营体系，擅长用数据公式拆解内容策略。核心方法论：结果=流量+转化，流量=数据+内容，转化=产品+路径',
    '⭐',
    'doubao',
    'doubao-seed-2-0-pro-260215',
    1,
    'writer',
    '公式化运营·数据诊断·选题策略·脚本结构'
)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    api_provider = EXCLUDED.api_provider,
    model_name = EXCLUDED.model_name,
    is_active = EXCLUDED.is_active,
    role = COALESCE(EXCLUDED.role, advisors.role),
    specialty = COALESCE(EXCLUDED.specialty, advisors.specialty),
    updated_at = NOW();

SELECT id, name, role, specialty FROM advisors WHERE id = '星壹';
