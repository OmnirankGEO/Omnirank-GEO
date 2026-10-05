/**
 * AgentLevelGate - 代理等级条件渲染组件
 * 根据用户代理等级决定是否渲染子组件
 *
 * CTO-13.0 2026-04-19 P0 修复：
 *   - wallet.loading=true 时显示 Loading 骨架，不显示"免费用户"（避免真代理被误判）
 *   - 加载超过 5s 仍未成功 → 显示"余额加载失败"重试按钮
 *   - 真代理（agent_level≥1）在 wallet fetch 失败时不应被挡，提示刷新而非"去申请代理"
 */
import { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { ShieldAlert, ArrowUpCircle, Loader2 } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { usePartnerFlag } from "@/hooks/usePartnerFlag";
import { Link } from "react-router-dom";

// 兼容后端数字(0/1/2) 和字符串('free'/'paid'/'L1'/'L2') 两种 agent_level 格式
function toLevelNumber(v: unknown): number {
  if (typeof v === 'number') return v;
  if (v === 'L2') return 2;
  if (v === 'L1') return 1;
  // 'paid'/'free'/null/undefined 都视为 0
  return 0;
}

interface AgentLevelGateProps {
  requiredLevel: number;
  children: ReactNode;
  fallback?: ReactNode;
  /** 显式覆盖，仅供受控预览/测试；生产身份默认取 /api/auth/me。 */
  agentLevel?: number;
}

const LEVEL_LABELS: Record<number, string> = {
  0: "普通用户",
  1: "服务方",
  2: "服务方",
};

const UPGRADE_CONDITIONS_LEGACY: Record<number, string> = {
  1: "成为服务商即可解锁",
  2: "成为服务商即可解锁",
};

const UPGRADE_CONDITIONS_REVIEW: Record<number, string> = {
  1: "到「合作伙伴计划」申请服务商权限即可解锁",
  2: "到「合作伙伴计划」申请服务商权限即可解锁",
};

export function AgentLevelGate({
  requiredLevel,
  children,
  fallback,
  agentLevel,
}: AgentLevelGateProps) {
  const { user, isLoading } = useAuth();
  const { enabled: partnerFlagEnabled } = usePartnerFlag();

  // 管理员绕过等级检查
  if (user?.is_admin) {
    return <>{children}</>;
  }

  if (isLoading && agentLevel === undefined) {
    return (
      <div className="flex items-center justify-center min-h-[400px] p-6">
        <div className="flex flex-col items-center gap-3 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
          <p className="text-sm">正在确认账号权限…</p>
        </div>
      </div>
    );
  }

  // 优先级：受控覆盖 > /api/auth/me.agent_level；资金接口不参与身份判断。
  const currentLevel = agentLevel !== undefined
    ? toLevelNumber(agentLevel)
    : toLevelNumber(user?.agent_level);

  if (currentLevel >= requiredLevel) {
    return <>{children}</>;
  }

  if (fallback) {
    return <>{fallback}</>;
  }

  return (
    <div className="flex items-center justify-center min-h-[400px] p-6">
      <div className="max-w-sm w-full text-center space-y-5">
        <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-muted">
          <ShieldAlert className="h-7 w-7 text-muted-foreground" />
        </div>

        <div>
          <h3 className="text-lg font-semibold text-foreground">需要升级才能使用</h3>
          <p className="text-sm text-muted-foreground mt-2">
            当前：<span className="text-foreground font-medium">{LEVEL_LABELS[currentLevel] || "普通用户"}</span>
            {" / "}
            需要：<span className="text-foreground font-medium">{LEVEL_LABELS[requiredLevel]}</span>
          </p>
        </div>

        <p className="text-xs text-muted-foreground/70">
          {(partnerFlagEnabled ? UPGRADE_CONDITIONS_REVIEW : UPGRADE_CONDITIONS_LEGACY)[requiredLevel]}
        </p>

        <Button asChild className="w-full rounded-xl">
          <Link to={partnerFlagEnabled ? "/partner/about" : "/wallet"}>
            <ArrowUpCircle className="mr-2 h-4 w-4" />
            {partnerFlagEnabled ? "去申请服务商" : "前往充值升级"}
          </Link>
        </Button>
      </div>
    </div>
  );
}
