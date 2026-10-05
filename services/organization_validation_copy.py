"""把 FastAPI/Pydantic 的 422 字段校验错误翻译成人话。

[P0 修复 2026-08-17 · 后端审计 §0-1 / §1]

为什么必须有这一层:`api/organization_api.py` 的每个请求模型都用 `Field(...)`
做长度/正则约束(验证码 `^[0-9]{6}$`、密码 `min_length`、团队代码 4-12 位、
token `min_length=20`、reason `min_length=1` ……)。这些约束由 FastAPI 在进 handler
**之前**拦下,抛的是 `RequestValidationError` —— detail 是一个**数组**,不是
`{code, message}`。前端 `organizationApi.responseDetail` 认不出数组,直接落到兜底
文案「组织服务暂不可用」。

后果就是本次「完全看不懂」反馈的头号来源:**员工在接受邀请页少输一位验证码,
看到的是「组织服务暂不可用」** —— 他以为系统挂了,实际是自己少打了一个数字。

修法:按**字段名**给人话(字段名是我们自己定的、稳定的),而不是去解析 Pydantic
的英文 `msg`(那是上游文案,会随版本变)。未登记的字段一律落到不含英文的通用文案,
原始字段名与错误类型进 `details` 供客服排查 —— `details` 不渲染给用户。

判据成对(工单 §5-3):
  - 必须命中:验证码输 5 位 → 「验证码是 6 位数字」,**不是**「组织服务暂不可用」;
  - 必须不命中:后端真 5xx → 仍然是服务不可用文案(别把故障翻译成输入错误)。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence


ORGANIZATION_PATH_PREFIXES = (
    "/api/organization",
    "/api/public/organization",
    "/api/admin/organization-product-config",
)

VALIDATION_ERROR_CODE = "ORG_REQUEST_FIELD_INVALID"

#: 兜底文案。不含任何英文字段名 —— 字段名进 details,不进 message。
GENERIC_COPY = "表单有一项没填对，请检查后重试"

#: 缺必填项时的兜底(比「格式不对」更贴切)。
GENERIC_MISSING_COPY = "有必填项没有填，请补齐后重试"

#: 字段名 → 人话。key 与 `api/organization_api.py` 的请求模型字段一一对应。
#: 值可以是一句话(所有错误类型同一句),或 {错误类型前缀: 文案} 细分。
FIELD_COPY: dict[str, Any] = {
    # 接受邀请 / 开户(公开面,每个新员工必经 —— 优先级最高)
    "code": "验证码是 6 位数字，请重新输入",
    "password": "密码至少 6 位，请重新设置",
    "display_name": "请填写你的姓名（不超过 80 字）",
    "terms_accepted": "请先勾选同意用户协议",
    "privacy_accepted": "请先勾选同意隐私政策",
    "terms_version": "页面数据已过期，请刷新后重试",
    "privacy_version": "页面数据已过期，请刷新后重试",
    "verification_receipt": "验证已失效，请重新获取验证码",
    "challenge_id": "验证已失效，请重新获取验证码",
    "token": "邀请链接不完整，请使用团队负责人发给你的完整链接打开",
    # 团队代码 / 登录名
    "short_code": "团队代码须为 4-12 位小写字母或数字",
    "member_name": "员工名须为 1-32 位小写字母、数字、下划线、点或连字符",
    # 团队与成员治理(老板面)
    "name": "团队名称不能为空，且不能超过 120 字",
    "reason": "请填写原因",
    "high_risk_reason": "请说明为什么要给他这些额外权限",
    "target": "请填写员工的手机号、邮箱或登录用户名",
    "target_kind": "邀请方式无效，请重新选择",
    "role_id": "请选择员工角色",
    "brand_ids": "客户选择无效，请重新勾选",
    "membership_id": "请选择员工",
    "from_membership_id": "请选择交出客户的员工",
    "to_membership_id": "请选择接手客户的员工",
    "action": "操作无效，请刷新页面重试",
    "decision": "审批结果无效，请重新选择",
    "capabilities": "权限选择无效，请重新勾选",
    "overrides": "权限选择无效，请重新勾选",
    "capability_overrides": "权限选择无效，请重新勾选",
    "artifact_scope": "「可查看内容范围」选择无效，请重新选择",
    "code_": "角色标识无效，请重新填写",
    # 上限 / 算力(全部按「算力上限」口径,禁「额度」)
    "limit_kind": "上限类型无效，请重新选择",
    "limit_points": "算力上限请填 0 或正整数",
    "daily_limit_points": "每日算力上限请填 0 或正整数",
    "monthly_limit_points": "每月算力上限请填 0 或正整数",
    "per_action_limit_points": "单次代付上限请填正整数",
    "estimated_points": "算力数值无效，请重新填写",
    "min_points": "算力范围无效，请重新填写",
    "max_points": "算力范围无效，请重新填写",
    "threshold_points": "需要审批的算力阈值请填正整数",
    "total_budget_points": "总预算请填正整数（单位：算力）",
    "max_occurrence_points": "单次上限请填正整数（单位：算力）",
    "cumulative_refund_target": "退款算力数无效，请重新填写",
    "daily": "功能日上限请填 0 或正整数",
    "monthly": "功能月上限请填 0 或正整数",
    "feature_code": "请选择功能",
    "feature_limits": "功能上限设置无效，请重新填写",
    # 自动计划
    "cadence_seconds": "间隔时间无效，请重新填写",
    "max_occurrences": "执行次数请填正整数",
    "starts_at": "计划开始时间无效，请重新选择",
    "ends_at": "计划结束时间无效，请重新选择",
    "custom_start": "起始时间无效，请重新选择",
    "custom_end": "结束时间无效，请重新选择",
    "work_kind": "计划类型无效，请刷新页面重试",
    "status": "状态无效，请刷新页面重试",
    # 分享 / 公开链接
    "artifact_type": "内容类型无效，请从对应内容页重新发起",
    "artifact_id": "内容无效，请从对应内容页重新发起",
    "purpose": "分享类型无效，请从对应内容页重新发起",
    "permission": "共享权限无效，请重新选择",
    "expires_in_seconds": "有效期无效，请重新选择",
    "public_scope": "分享范围无效，请重新选择",
    # 通用协议字段
    "request_id": "请求编号无效，请刷新页面后重试",
    "refund_request_id": "请求编号无效，请刷新页面后重试",
    "expected_version": "页面数据已过期，请刷新后重试",
    "expected_membership_version": "页面数据已过期，请刷新后重试",
    "brand_id": "客户无效，请重新选择",
    "action_type": "操作类型无效，请刷新页面重试",
    "payload": "提交内容无效，请刷新页面重试",
    "execution_id": "请求编号无效，请刷新页面后重试",
    # 平台管理员面(组织席位商品配置)
    "included_seats": "基础席位数请填正整数",
    "invite_ttl_hours": "邀请有效期请填正整数（小时）",
    "verification_ttl_minutes": "验证码有效期请填正整数（分钟）",
    "verification_max_attempts": "验证码尝试次数请填正整数",
}


def is_organization_path(path: str) -> bool:
    value = str(path or "")
    return any(
        value == prefix or value.startswith(prefix + "/")
        for prefix in ORGANIZATION_PATH_PREFIXES
    )


def _leaf_field(location: Sequence[Any]) -> Optional[str]:
    """取错误位置里最后一个**字符串**段 —— 那就是字段名。

    `loc` 形如 `("body", "code")`、`("body", "feature_limits", "monitor_single", "daily")`;
    数组下标是 int,要跳过。`("body",)` 本身(整个 body 不是对象)返回 None。
    """
    for item in reversed(list(location or ())):
        if isinstance(item, str) and item not in {"body", "query", "path", "header", "cookie"}:
            return item
    return None


def _copy_for(field: Optional[str], error_type: str) -> Optional[str]:
    if not field:
        return None
    entry = FIELD_COPY.get(field)
    if entry is None:
        return None
    if isinstance(entry, Mapping):
        for prefix, text in entry.items():
            if str(error_type).startswith(str(prefix)):
                return str(text)
        return None
    return str(entry)


def validation_message(errors: Iterable[Mapping[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """返回 (用户看到的一句话, 机读字段清单)。

    多个字段同时出错时只讲**第一个能讲清的**——一次让用户改一处,比甩一串更可用;
    完整清单进 `details.fields`。
    """
    machine: list[dict[str, str]] = []
    chosen: Optional[str] = None
    missing_only = True
    for error in errors or ():
        error_type = str(error.get("type") or "")
        field = _leaf_field(error.get("loc") or ())
        machine.append({"field": field or "body", "type": error_type})
        if error_type != "missing":
            missing_only = False
        text = _copy_for(field, error_type)
        if text and chosen is None:
            chosen = text
    if chosen is None:
        chosen = GENERIC_MISSING_COPY if (machine and missing_only) else GENERIC_COPY
    return chosen, machine


def validation_detail(
    errors: Iterable[Mapping[str, Any]],
    *,
    request_id: str,
) -> dict[str, Any]:
    """构造与 `OrganizationError.as_detail` **同形**的 detail,前端一套解析吃两种。"""
    message, machine = validation_message(errors)
    return {
        "code": VALIDATION_ERROR_CODE,
        "message": message,
        "request_id": request_id,
        "retryable": False,
        "details": {"fields": machine},
    }
