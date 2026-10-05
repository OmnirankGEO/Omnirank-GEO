/**
 * AgentCluster — 多角色协作动画组件
 * 展示 AI 团队成员的工作状态，让用户看到"一群人在帮你干活"
 */
import { motion } from 'framer-motion';
import { cn } from '@/lib/utils';

import defaultAgentAvatar from '@/assets/logo-icon-light.png';

interface AgentProfile {
  name: string;
  title: string;
  avatar: string;
  doneQuote: string;
}

const AGENT_PROFILES: Record<string, AgentProfile> = {
  researcher:    { name: '小研', title: '行业趋势分析师', avatar: defaultAgentAvatar, doneQuote: '趋势摸清了' },
  audience:      { name: '小洞', title: '受众心理专家',   avatar: defaultAgentAvatar, doneQuote: '痛点抓到了！' },
  knowledge:     { name: '小知', title: '知识库管理员',   avatar: defaultAgentAvatar, doneQuote: '资料找到了' },
  planner:       { name: '策策', title: '首席策划师',     avatar: defaultAgentAvatar, doneQuote: '选题出炉！' },
  hook_designer: { name: '勾勾', title: '开头杀手',       avatar: defaultAgentAvatar, doneQuote: '钩子设计好了' },
  writer:        { name: '文文', title: '脚本架构师',     avatar: defaultAgentAvatar, doneQuote: '结构搭好了' },
  style:         { name: '向向', title: '风格模仿师',     avatar: defaultAgentAvatar, doneQuote: '学会你说话了~' },
  deepseek:      { name: '小深', title: 'DeepSeek 探员', avatar: defaultAgentAvatar, doneQuote: '在DS里找到你了' },
  kimi:          { name: '小月', title: 'Kimi 探员',     avatar: defaultAgentAvatar, doneQuote: '月球报告完毕~' },
  doubao:        { name: '小麦', title: '豆包探员',       avatar: defaultAgentAvatar, doneQuote: '豆豆检测完毕！' },
  tongyi:        { name: '小通', title: '通义探员',       avatar: defaultAgentAvatar, doneQuote: '千问分析已出' },
  reviewer:      { name: '评评', title: '诊断报告官',     avatar: defaultAgentAvatar, doneQuote: '报告已就绪' },
};

export interface AgentStatus {
  role: string;
  task: string;
  status: 'waiting' | 'working' | 'done';
  result?: string;
}

interface AgentClusterProps {
  agents: AgentStatus[];
  className?: string;
}

export function AgentCluster({ agents, className }: AgentClusterProps) {
  return (
    <div className={cn('space-y-2', className)}>
      {agents.map((agent, i) => {
        const profile = AGENT_PROFILES[agent.role];
        if (!profile) return null;

        return (
          <motion.div
            key={agent.role}
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: i * 0.12, type: 'spring', damping: 20 }}
            className={cn(
              'rounded-xl border p-3 flex items-start gap-3 transition-colors',
              agent.status === 'working' && 'border-foreground/15 bg-card',
              agent.status === 'done' && 'border-border bg-card/50',
              agent.status === 'waiting' && 'border-border/50 bg-transparent opacity-40',
            )}
          >
            {/* Avatar */}
            <motion.img
              src={profile.avatar}
              alt={profile.name}
              initial={{ scale: 0 }}
              animate={{ scale: 1 }}
              transition={{ type: 'spring', damping: 12, stiffness: 300, delay: i * 0.12 }}
              className={cn(
                'h-9 w-9 rounded-full object-cover shrink-0',
                agent.status === 'working' && 'ring-2 ring-brand/40',
              )}
            />

            {/* Info */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-foreground">{profile.name}</span>
                <span className="text-xs text-muted-foreground">{profile.title}</span>
                <span className="ml-auto shrink-0">
                  {agent.status === 'working' && (
                    <motion.span
                      className="inline-block h-1.5 w-1.5 rounded-full bg-green-400"
                      animate={{ opacity: [1, 0.3, 1] }}
                      transition={{ repeat: Infinity, duration: 1.2 }}
                    />
                  )}
                  {agent.status === 'done' && (
                    <motion.span
                      initial={{ scale: 0 }}
                      animate={{ scale: [0, 1.2, 1] }}
                      transition={{ duration: 0.3 }}
                      className="text-xs text-green-400"
                    >
                      ✓
                    </motion.span>
                  )}
                </span>
              </div>
              <p className="text-xs text-muted-foreground mt-0.5">
                {agent.status === 'done'
                  ? <span className="italic text-muted-foreground/70">"{agent.result || profile.doneQuote}"</span>
                  : agent.task}
              </p>
            </div>
          </motion.div>
        );
      })}
    </div>
  );
}
