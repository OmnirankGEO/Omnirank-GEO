/**
 * 彩带庆祝 · 封装 canvas-confetti
 * 用于新手教程"全部完成"的仪式感(以及任何里程碑庆祝)。
 */
import confetti from 'canvas-confetti';

/** 大庆祝 · 两侧礼炮 + 顶部撒花 · 约 2.5 秒 */
export function fireBigCelebration() {
    const duration = 2200;
    const end = Date.now() + duration;
    const colors = ['#f59e0b', '#fb923c', '#22c55e', '#3b82f6', '#a855f7'];

    // 中心一发
    confetti({ particleCount: 120, spread: 90, startVelocity: 45, origin: { y: 0.6 }, colors });

    // 两侧持续小礼炮
    (function frame() {
        confetti({ particleCount: 4, angle: 60, spread: 55, origin: { x: 0 }, colors });
        confetti({ particleCount: 4, angle: 120, spread: 55, origin: { x: 1 }, colors });
        if (Date.now() < end) requestAnimationFrame(frame);
    })();
}

/** 轻庆祝 · 单发小撒花 · 用于每步完成 */
export function fireSmallCelebration() {
    confetti({
        particleCount: 50,
        spread: 60,
        startVelocity: 35,
        origin: { y: 0.5 },
        colors: ['#f59e0b', '#fb923c', '#22c55e'],
        scalar: 0.9,
    });
}
