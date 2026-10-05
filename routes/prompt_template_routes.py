"""
Prompt模板管理 API 路由
提供REST API供前端调用
"""
from flask import Blueprint, request, jsonify
from writing.prompt_template_manager import get_template_manager, PromptTemplate

# 创建蓝图
prompt_template_bp = Blueprint('prompt_templates', __name__, url_prefix='/api/prompt-templates')

# ============ 模板管理 API ============

@prompt_template_bp.route('/', methods=['GET'])
def list_templates():
    """获取所有模板列表"""
    manager = get_template_manager()
    category = request.args.get('category')
    templates = manager.list_templates(category)
    
    result = []
    for t in templates:
        active_version = manager.get_active_version(t.template_id)
        result.append({
            "template_id": t.template_id,
            "template_name": t.template_name,
            "template_code": t.template_code,
            "category": t.category,
            "description": t.description,
            "target_score": t.target_score,
            "version_count": len(t.versions),
            "current_version": active_version.version_name if active_version else None,
            "avg_score": active_version.avg_score if active_version else 0,
            "created_at": t.created_at,
            "updated_at": t.updated_at
        })
    
    return jsonify({"success": True, "data": result})


@prompt_template_bp.route('/<template_id>', methods=['GET'])
def get_template(template_id: str):
    """获取模板详情"""
    manager = get_template_manager()
    template = manager.get_template(template_id)
    
    if not template:
        return jsonify({"success": False, "error": "模板不存在"}), 404
    
    return jsonify({
        "success": True,
        "data": {
            "template_id": template.template_id,
            "template_name": template.template_name,
            "template_code": template.template_code,
            "category": template.category,
            "description": template.description,
            "target_score": template.target_score,
            "current_version_id": template.current_version_id,
            "versions": [
                {
                    "version_id": v.version_id,
                    "version_name": v.version_name,
                    "is_active": v.is_active,
                    "avg_score": v.avg_score,
                    "test_count": v.test_count,
                    "notes": v.notes,
                    "created_at": v.created_at
                }
                for v in template.versions
            ],
            "created_at": template.created_at,
            "updated_at": template.updated_at
        }
    })


@prompt_template_bp.route('/', methods=['POST'])
def create_template():
    """创建新模板"""
    data = request.get_json()
    manager = get_template_manager()
    
    required_fields = ['template_name', 'template_code', 'category', 'description', 'initial_prompt']
    for field in required_fields:
        if field not in data:
            return jsonify({"success": False, "error": f"缺少必填字段: {field}"}), 400
    
    template = manager.create_template(
        template_name=data['template_name'],
        template_code=data['template_code'],
        category=data['category'],
        description=data['description'],
        initial_prompt=data['initial_prompt'],
        version_name=data.get('version_name', 'v1.0')
    )
    
    return jsonify({
        "success": True,
        "data": {
            "template_id": template.template_id,
            "template_name": template.template_name
        }
    })


@prompt_template_bp.route('/<template_id>', methods=['PUT'])
def update_template(template_id: str):
    """更新模板基本信息"""
    data = request.get_json()
    manager = get_template_manager()
    
    template = manager.update_template(template_id, **data)
    if not template:
        return jsonify({"success": False, "error": "模板不存在"}), 404
    
    return jsonify({"success": True, "message": "更新成功"})


@prompt_template_bp.route('/<template_id>', methods=['DELETE'])
def delete_template(template_id: str):
    """删除模板"""
    manager = get_template_manager()
    success = manager.delete_template(template_id)
    
    if not success:
        return jsonify({"success": False, "error": "模板不存在"}), 404
    
    return jsonify({"success": True, "message": "删除成功"})


# ============ 版本管理 API ============

@prompt_template_bp.route('/<template_id>/versions', methods=['GET'])
def get_versions(template_id: str):
    """获取模板的所有版本"""
    manager = get_template_manager()
    template = manager.get_template(template_id)
    
    if not template:
        return jsonify({"success": False, "error": "模板不存在"}), 404
    
    return jsonify({
        "success": True,
        "data": [
            {
                "version_id": v.version_id,
                "version_name": v.version_name,
                "is_active": v.is_active,
                "avg_score": v.avg_score,
                "test_count": v.test_count,
                "notes": v.notes,
                "created_at": v.created_at
            }
            for v in template.versions
        ]
    })


@prompt_template_bp.route('/<template_id>/versions/<version_id>', methods=['GET'])
def get_version_detail(template_id: str, version_id: str):
    """获取版本详情（含Prompt内容）"""
    manager = get_template_manager()
    version = manager.get_version(template_id, version_id)
    
    if not version:
        return jsonify({"success": False, "error": "版本不存在"}), 404
    
    stats = manager.get_version_stats(template_id, version_id)
    
    return jsonify({
        "success": True,
        "data": {
            "version_id": version.version_id,
            "version_name": version.version_name,
            "prompt_content": version.prompt_content,
            "is_active": version.is_active,
            "avg_score": version.avg_score,
            "test_count": version.test_count,
            "notes": version.notes,
            "created_at": version.created_at,
            "stats": stats
        }
    })


@prompt_template_bp.route('/<template_id>/versions', methods=['POST'])
def add_version(template_id: str):
    """添加新版本"""
    data = request.get_json()
    manager = get_template_manager()
    
    if 'version_name' not in data or 'prompt_content' not in data:
        return jsonify({"success": False, "error": "缺少必填字段"}), 400
    
    version = manager.add_version(
        template_id=template_id,
        version_name=data['version_name'],
        prompt_content=data['prompt_content'],
        notes=data.get('notes', ''),
        set_active=data.get('set_active', False)
    )
    
    if not version:
        return jsonify({"success": False, "error": "模板不存在"}), 404
    
    return jsonify({
        "success": True,
        "data": {
            "version_id": version.version_id,
            "version_name": version.version_name
        }
    })


@prompt_template_bp.route('/<template_id>/versions/<version_id>/activate', methods=['POST'])
def activate_version(template_id: str, version_id: str):
    """设置版本为生产版本"""
    manager = get_template_manager()
    success = manager.set_active_version(template_id, version_id)
    
    if not success:
        return jsonify({"success": False, "error": "版本不存在"}), 404
    
    return jsonify({"success": True, "message": "版本已激活"})


# ============ A/B测试 API ============

@prompt_template_bp.route('/ab-tests', methods=['GET'])
def list_ab_tests():
    """获取所有A/B测试"""
    manager = get_template_manager()
    
    result = []
    for ab in manager.ab_tests.values():
        stats = manager.get_ab_test_stats(ab.test_id)
        result.append(stats)
    
    return jsonify({"success": True, "data": result})


@prompt_template_bp.route('/ab-tests', methods=['POST'])
def create_ab_test():
    """创建A/B测试"""
    data = request.get_json()
    manager = get_template_manager()
    
    required_fields = ['test_name', 'template_id', 'version_a_id', 'version_b_id']
    for field in required_fields:
        if field not in data:
            return jsonify({"success": False, "error": f"缺少必填字段: {field}"}), 400
    
    ab_test = manager.create_ab_test(
        test_name=data['test_name'],
        template_id=data['template_id'],
        version_a_id=data['version_a_id'],
        version_b_id=data['version_b_id']
    )
    
    return jsonify({
        "success": True,
        "data": {"test_id": ab_test.test_id}
    })


@prompt_template_bp.route('/ab-tests/<test_id>', methods=['GET'])
def get_ab_test(test_id: str):
    """获取A/B测试详情"""
    manager = get_template_manager()
    stats = manager.get_ab_test_stats(test_id)
    
    if not stats:
        return jsonify({"success": False, "error": "测试不存在"}), 404
    
    return jsonify({"success": True, "data": stats})


@prompt_template_bp.route('/ab-tests/<test_id>/complete', methods=['POST'])
def complete_ab_test(test_id: str):
    """完成A/B测试"""
    manager = get_template_manager()
    winner = manager.complete_ab_test(test_id)
    
    if not winner:
        return jsonify({"success": False, "error": "测试不存在"}), 404
    
    return jsonify({
        "success": True,
        "data": {"winner": winner}
    })


# ============ 评分历史 API ============

@prompt_template_bp.route('/score-history', methods=['GET'])
def get_score_history():
    """获取评分历史"""
    manager = get_template_manager()
    template_id = request.args.get('template_id')
    version_id = request.args.get('version_id')
    limit = int(request.args.get('limit', 50))
    
    records = manager.get_score_history(template_id, version_id, limit)
    
    return jsonify({
        "success": True,
        "data": [
            {
                "record_id": r.record_id,
                "template_id": r.template_id,
                "version_id": r.version_id,
                "article_title": r.article_title,
                "score": r.score,
                "dimension_scores": r.dimension_scores,
                "created_at": r.created_at
            }
            for r in records
        ]
    })


@prompt_template_bp.route('/score-history', methods=['POST'])
def record_score():
    """记录评分"""
    data = request.get_json()
    manager = get_template_manager()
    
    required_fields = ['template_id', 'version_id', 'article_title', 'score', 'dimension_scores', 'review_text']
    for field in required_fields:
        if field not in data:
            return jsonify({"success": False, "error": f"缺少必填字段: {field}"}), 400
    
    record = manager.record_score(
        template_id=data['template_id'],
        version_id=data['version_id'],
        article_title=data['article_title'],
        score=data['score'],
        dimension_scores=data['dimension_scores'],
        review_text=data['review_text'],
        reviewer_model=data.get('reviewer_model', 'gemini-3-pro-preview')
    )
    
    return jsonify({
        "success": True,
        "data": {"record_id": record.record_id}
    })


# ============ 写作风格 API ============

@prompt_template_bp.route('/styles', methods=['GET'])
def list_styles():
    """获取所有可用的写作风格（三种：V2.0/V9/特洛伊）"""
    from writing.style_registry import get_styles_for_frontend
    
    styles = get_styles_for_frontend()
    return jsonify({
        "success": True,
        "data": styles
    })


@prompt_template_bp.route('/styles/ratios', methods=['GET'])
def get_style_ratios():
    """获取风格配比"""
    from writing.style_registry import get_style_ratios
    
    ratios = get_style_ratios()
    return jsonify({
        "success": True,
        "data": ratios
    })


@prompt_template_bp.route('/styles/ratios', methods=['PUT'])
def update_style_ratios():
    """更新风格配比（前端调整时使用）"""
    from writing.style_registry import update_style_ratios as do_update
    
    data = request.get_json()
    new_ratios = data.get('ratios', {})
    
    # 验证配比总和为100
    if sum(new_ratios.values()) != 100:
        return jsonify({
            "success": False, 
            "error": "配比总和必须为100"
        }), 400
    
    success = do_update(new_ratios)
    return jsonify({
        "success": success,
        "message": "配比更新成功" if success else "更新失败"
    })


@prompt_template_bp.route('/styles/allocate', methods=['POST'])
def allocate_styles():
    """根据配比分配风格（用于批量文章生成）"""
    from writing.style_registry import allocate_styles_by_ratio, WRITING_STYLES
    
    data = request.get_json()
    count = data.get('count', 10)
    
    allocation = allocate_styles_by_ratio(count)
    
    # 返回详细分配结果
    result = [
        {
            "index": i + 1,
            "style_code": code,
            "style_name": WRITING_STYLES[code]["name"]
        }
        for i, code in enumerate(allocation)
    ]
    
    return jsonify({
        "success": True,
        "data": {
            "count": count,
            "allocation": result
        }
    })

