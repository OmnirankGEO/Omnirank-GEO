/**
 * PrivacyNotice — 公开页轻提示 (CTO-C 2026-04-26)
 *
 * 老板拍板文案:
 *   "为便于顾问跟进服务,本页会记录打开与停留状态。"
 *
 * 不写"监控""追踪"这类词。不弹窗。不勾选。
 * 不影响 5 公开 token 链路 HTTP 200 + 正常渲染。
 */

export function PrivacyNotice({ className = '' }: { className?: string }) {
  return (
    <p
      className={
        'text-[10px] text-muted-foreground/60 leading-relaxed text-center ' + className
      }
    >
      为便于顾问跟进服务，本页会记录打开与停留状态。
    </p>
  );
}
