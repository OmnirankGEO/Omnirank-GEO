/**
 * DeductDialog — P5b 全站静默扣费(2026-06-03 改:去前置确认)
 *
 * 历史:这是"确认才扣"的前置弹窗(用户必须点「确认使用 N 额度」才执行)。
 * P5b 改造:**去前置确认** —— `open` 变 true = 直接执行扣费动作(onConfirm),不再等用户点确认。
 *   · 扣费逻辑本身不变(deductPoints 仍走后端 /api/wallet/deduct 单一入口)
 *   · 扣完按用户「扣费提醒设置」偏好后置通知(WalletContext.notifyCharge,在 deductPoints 里统一触发)
 *   · 各调用页业务流不变:仍是 `setShowDeductDialog(true)` 触发,只是语义从"开弹窗"变"直接执行"
 *
 * 唯一仍会"挡一下"的情况:**余额不足**(铁律:不静默,必须提示充值) → 渲染 InsufficientDialog。
 *
 * 王姐文案铁律:禁"静默扣费/冻结/流水/billing" · "积分"一律叫「算力」(开发原则 SSOT B4)。
 */
import { useEffect, useRef } from 'react';
import { useWallet } from '@/context/WalletContext';
import { useAuth } from '@/context/AuthContext';
import { useSandboxState } from '@/sandbox/sandboxState';
import InsufficientDialog from '@/components/InsufficientDialog';
import { WalletUnavailableDialog } from '@/components/wallet/WalletUnavailableDialog';

// [V3.5 v8 P0 2026-06-08 Codex 复审]发布类 feature 判定 · 对齐后端 services/customer_credit.is_publish_feature
// 后端 PUBLISH_FEATURE_CODES + publish_ 前缀 · V3.5 客户 publish_credit 隔离不可用 tool/bonus 跑发布
const PUBLISH_FEATURE_CODES = new Set([
  'publish_mhz_media',
  'publish_single',
  'publish_batch',
  'publish_wemedia',
]);
function isPublishFeature(featureCode: string): boolean {
  return PUBLISH_FEATURE_CODES.has(featureCode) || featureCode.startsWith('publish_');
}

interface DeductDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  featureCode: string;
  featureName: string;
  cost: number;
  onConfirm: () => void;
  loading?: boolean;
}

export default function DeductDialog({
  open,
  onOpenChange,
  featureCode,
  featureName: _featureName,
  cost,
  onConfirm,
  loading: _loading = false,
}: DeductDialogProps) {
  const { toolPointsAvailable, publishPointsAvailable, status: walletStatus } = useWallet();
  const { user } = useAuth();
  const { isSandbox } = useSandboxState();
  const isAdmin = user?.is_admin === true;

  // [V3.5 v8 P0]按 feature 类型选预检池 · publish_xxx 走 publish_credit · 其他走 tool_credit + bonus
  // 老 totalPoints 含 publish 误并 · V3.5 客户 publish>>tool 时工具预检会误判"够" 后端 402
  const availableForFeature = isPublishFeature(featureCode) ? publishPointsAvailable : toolPointsAvailable;
  // [沙盒卡死修复 2026-07-27] 教程模式不做余额预检。
  //   根因:`/api/wallet` 在 SANDBOX_EXACT_BYPASS 里(沙盒读【真实】余额,这是对的 ——
  //   教程里钱包页显示假余额会更糟),但沙盒的扣费 mutation 全被 sandboxInterceptor 拦下,
  //   **永远不会真扣**。于是这里拿真实余额去挡一个在沙盒中不存在的约束 ——
  //   真实余额低的代理(生产实测 638 < 650)直接被自己的余额挡在新手教程外,走不完流程。
  //   与 isAdmin 同级处理:不是"绕过计费",而是"这条路径根本不产生计费"。
  const insufficient = !isAdmin && !isSandbox && availableForFeature < cost;
  // 防同一次 open 内重复触发动作(React StrictMode 双调 / 父级重渲染)
  const firedRef = useRef(false);

  useEffect(() => {
    if (!open) {
      firedRef.current = false;
      return;
    }
    if (walletStatus !== 'ready') return;
    if (insufficient) return; // 余额不足 → 走 InsufficientDialog,不执行动作
    if (firedRef.current) return;
    firedRef.current = true;
    // 去前置确认:打开即直接执行扣费动作(扣费逻辑不变,扣完按偏好通知)
    onConfirm();
    // 动作已发起 → 立即收起本"开关"状态,业务流交给调用页自己的 loading
    onOpenChange(false);
    // onConfirm/onOpenChange 入 deps 满足 lint · firedRef 保证同一 open 只触发一次,即使它们变化也不会重复扣
  }, [open, walletStatus, insufficient, onConfirm, onOpenChange]);

  if (walletStatus !== 'ready') {
    return <WalletUnavailableDialog open={open} onOpenChange={onOpenChange} />;
  }

  // 余额不足:保留提示(不静默,必须引导充值)
  if (insufficient) {
    return (
      <InsufficientDialog
        open={open}
        onOpenChange={onOpenChange}
        needed={cost}
        currentBalance={availableForFeature}
      />
    );
  }

  // 余额充足:无 UI(扣费动作已在 effect 里直接发起)
  return null;
}
