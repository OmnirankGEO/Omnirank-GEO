/**
 * SandboxVideoSlot · 沙盒教程"客户端操作"聚焦录屏
 *
 * 用于"代理等客户操作"的步骤(客户选业务方向 / 客户选套餐)· 让代理看懂客户在他那端做了啥。
 *
 * 行为(老板要求):
 *   - 全屏遮罩 + 居中卡片 · 聚焦在视频上, 不和周围文字抢位置
 *   - 进入这一步自动播放(muted 保证浏览器允许自动播放)· 播一遍, 不循环
 *   - 视频播完(onEnded)→ 先显示"客户已提交 ✓"约 1.6s → 再调 onComplete 推进到下一步
 *   - 视频文件还没录好(404)→ 切占位 · 给"模拟客户完成"按钮替代 onEnded
 *   - 同事把 mp4 放进 public/sandbox/ 后, 刷新即自动播放 · 无需改代码
 */
import { useEffect, useRef, useState } from 'react';
import { Video, Check } from 'lucide-react';

interface Props {
    /** 视频路径 · 放在 public/sandbox/ 下 · 如 /sandbox/client-select-business.mp4 */
    src: string;
    /** 头部标题 · 如 "选业务方向" */
    title: string;
    /** 视频下方说明文案 · 解释这一步客户在做什么 */
    hint: string;
    /** 完成态主标题 · 如 "客户已提交选词" */
    doneLabel: string;
    /** 完成态副标题 · 如 "正在进入「计算报价」…" */
    doneSub: string;
    /** 视频播完(或占位"模拟完成")后调用 · 由调用方推进沙盒状态机 + 刷新 */
    onComplete: () => void;
}

export function SandboxVideoSlot({ src, title, hint, doneLabel, doneSub, onComplete }: Props) {
    const [failed, setFailed] = useState(false);
    const [done, setDone] = useState(false);
    const firedRef = useRef(false);

    // 播完(或点占位继续)→ 进完成态
    const finish = () => {
        if (firedRef.current) return;
        firedRef.current = true;
        setDone(true);
    };

    // 完成态停留 1.6s 让代理看清"客户已提交" → 再推进
    useEffect(() => {
        if (!done) return;
        const t = setTimeout(() => onComplete(), 1600);
        return () => clearTimeout(t);
    }, [done, onComplete]);

    return (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
            <div className="w-full max-w-5xl overflow-hidden rounded-2xl border border-border bg-card shadow-2xl">
                <div className="flex items-center gap-2 border-b border-border bg-muted/30 px-5 py-3">
                    <Video className="h-4 w-4 text-brand" />
                    <span className="text-sm font-medium">客户端实拍 · {title}</span>
                    <span className="ml-auto text-xs text-muted-foreground">这是客户在他那边的操作</span>
                </div>

                {done ? (
                    <div className="flex flex-col items-center justify-center gap-3 py-16">
                        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-green-500/15">
                            <Check className="h-7 w-7 text-green-500" />
                        </div>
                        <div className="text-base font-semibold text-foreground">{doneLabel}</div>
                        <div className="text-sm text-muted-foreground">{doneSub}</div>
                    </div>
                ) : !failed ? (
                    <video
                        src={src}
                        autoPlay
                        muted
                        playsInline
                        controls
                        className="aspect-video w-full bg-black"
                        onEnded={finish}
                        onError={() => setFailed(true)}
                    />
                ) : (
                    <div className="m-4 flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed border-amber-400/40 bg-amber-50/40 px-6 py-12 text-center dark:bg-amber-950/20">
                        <Video className="h-8 w-8 text-amber-500" />
                        <div className="text-sm font-medium text-amber-700 dark:text-amber-300">📹 {title} · 录屏录制中</div>
                        <div className="text-[11px] text-muted-foreground/70">
                            视频放到 <span className="font-mono">public{src}</span> 后刷新即自动播放
                        </div>
                        <button
                            onClick={finish}
                            className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white hover:opacity-90"
                        >
                            模拟客户完成 · 继续
                        </button>
                    </div>
                )}

                {!done && (
                    <div className="border-t border-border bg-muted/20 px-5 py-3 text-xs leading-5 text-muted-foreground">
                        {hint}
                    </div>
                )}
            </div>
        </div>
    );
}

export default SandboxVideoSlot;
