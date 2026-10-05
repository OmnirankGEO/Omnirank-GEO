"""WP4/WP6 · 图文合同链新端点的**统一入口闸**(规格 02 §13 / §9)。

## 三件事必须发生在任何副作用之前

顺序不可调,每一步都可能终止请求:

  1. **schema readiness** → 未就绪返回 503 `SCHEMA_NOT_READY`,
     且保证零 claim / 零资金 / 零订单 / 零 provider(规格 02 §13 末);
  2. **demo 只读** → 任何 mutation 返回 403 `DEMO_READ_ONLY`,
     零 DB 写、零外调、零 freeze(规格 §9 末 + D10);
  3. **能力级授权** → 写作/制作与发布**分别**授权(规格 §9)。

## 为什么闸必须在入口而不是在写入处

「未就绪时零副作用」这句话,只有当检查发生在**任何**副作用之前才成立。
放在写入处等于:claim 已经做了、算力已经冻了,然后才发现 schema 没就绪 ——
那时候"零副作用"只是文案。

## 为什么不挂 FLEET_SCHEMA_GUARDS

规格 02 §13 明令:本期不得把图文 readiness 加进无条件 fleet guard,
否则一个**新 lane** 没就绪会把诊断/报价/监测/发布全站拒启。
正确形态是"只有图文新 route 在入口复核",legacy route 完全不受影响。
"""
from __future__ import annotations

from typing import Any, Callable, Final, Optional

#: 本闸覆盖的新端点(与 02 §9.0 登记表同一份清单)。
#: 🔴 清单写在这里是为了让「新加端点要不要过闸」变成一个**看得见**的决定:
#:    漏加的端点在 test_every_new_endpoint_is_guarded 里会被点名。
GUARDED_MUTATION_ENDPOINTS: Final[tuple[str, ...]] = (
    "/api/geo-douyin/production-preview",
    "/api/geo-douyin/production-drafts",
    "/api/geo-douyin/batches",
    "/api/geo-douyin/posts/{post_id}/prepare-publish-media",
    "/api/meijiehezi/image-notes/publish-preview",
    "/api/meijiehezi/image-notes/publish-batch",
)

ABILITY_PRODUCE: Final = "writing.generate"
ABILITY_PUBLISH: Final = "publish.execute"


class SchemaNotReady(RuntimeError):
    """图文 lane 的 schema 未就绪。503,零副作用。"""

    def __init__(self, blockers: list[str]):
        super().__init__("GEO_IMAGE_NOTE_SCHEMA_NOT_READY")
        self.blockers = list(blockers)


class DemoReadOnly(RuntimeError):
    """演示模式下的写尝试。403,零 DB 写 / 零外调 / 零 freeze。"""

    def __init__(self, action: str):
        super().__init__(f"DEMO_READ_ONLY:{action}")
        self.action = action


class AbilityNotGranted(RuntimeError):
    """没有该能力(默认销售没有写作/发布)。403 + 「交给交付同事 / 申请权限」。"""

    def __init__(self, ability: str):
        super().__init__(f"ABILITY_NOT_GRANTED:{ability}")
        self.ability = ability


def error_payload(exc: Exception) -> dict[str, Any]:
    """统一错误合同(规格 §12)。

    🔴 `actions[].type` 只用现役 builder 认的枚举(`retry|nav|contact|dismiss|api`),
       不写会被 builder 静默丢弃的 `kind` —— 那会让用户看到一个没有按钮的错误。
    """
    if isinstance(exc, SchemaNotReady):
        return {
            "code": "SCHEMA_NOT_READY",
            "message": "这个功能还在开通中",
            "reason": "图文制作与发布所需的数据结构尚未就绪",
            "impact": "本次未创建任何任务,未冻结算力,未联系发布渠道",
            "repair_hint": "联系管理员核对开通状态",
            "actions": [{"id": "contact_admin", "label": "联系管理员", "type": "contact"}],
            "next_action": "contact_admin",
            "retryable": False,
            "h0_category": "transaction_and_funding",
            "governance_ref": "§11.1/H0/幂等-租约-CAS-事务-资金守恒",
            "rule_id": None,
            "rule_version": "geo-image-note-schema-v1",
            "blockers": exc.blockers[:20],
        }
    if isinstance(exc, DemoReadOnly):
        return {
            "code": "DEMO_READ_ONLY",
            "message": "演示模式 · 仅查看",
            "reason": "演示授权只提供只读投影",
            "impact": "本次未产生任何数据变更、外部调用或算力扣减",
            "repair_hint": "如需实际操作请使用你自己的账户",
            "actions": [{"id": "dismiss", "label": "知道了", "type": "dismiss"}],
            "next_action": "dismiss",
            "retryable": False,
            "h0_category": "tenant_and_authorization",
            "governance_ref": "§11.1/H0/跨租户、越权和敏感数据泄露",
            "rule_id": None,
            "rule_version": "demo-access-d10",
        }
    if isinstance(exc, AbilityNotGranted):
        return {
            "code": "ABILITY_NOT_GRANTED",
            "message": "你还没有这个操作权限",
            "reason": "该操作需要单独授权",
            "impact": "本次未产生任何变更",
            "repair_hint": "交给交付同事,或向团队负责人申请权限",
            "actions": [
                {"id": "handoff", "label": "交给交付同事", "type": "nav"},
                {"id": "request_ability", "label": "申请权限", "type": "api"},
            ],
            "next_action": "handoff",
            "retryable": False,
            "h0_category": "tenant_and_authorization",
            "governance_ref": "§11.1/H0/跨租户、越权和敏感数据泄露",
            "rule_id": None,
            "rule_version": "organization-contract-capabilities",
        }
    raise TypeError(f"unmapped guard exception: {type(exc).__name__}")


def guard_mutation(*, schema_blockers_fn: Callable[[], list[str]],
                   is_demo: bool, action: str,
                   granted_abilities: Optional[set[str]] = None,
                   required_ability: Optional[str] = None) -> None:
    """在**任何**副作用之前调用。三步顺序固定。

    🔴 顺序为什么是「schema → demo → ability」:
       schema 未就绪时,连"你有没有权限"都无从谈起(读权限也要查表);
       demo 排在 ability 之前,是因为演示用户可能**恰好**持有该能力
       (他是被授权看某个品牌的真实用户),那时按 ability 放行就会真写库。
    """
    blockers = schema_blockers_fn()
    if blockers:
        raise SchemaNotReady(blockers)

    if is_demo:
        raise DemoReadOnly(action)

    if required_ability is not None:
        if not granted_abilities or required_ability not in granted_abilities:
            raise AbilityNotGranted(required_ability)
