/** Display a duration, never a deadline. Unknown numeric units are not guessed. */
export function publicationTime(value: unknown, numericUnit: 'seconds' | 'unknown' = 'unknown'): string {
    if (value === null || value === undefined || value === '') return '暂无数据';
    const raw = String(value).trim();
    if (!raw || /^(?:nan|infinity|-infinity|null|undefined)$/i.test(raw)) return '暂无数据';
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(raw)) return raw;
    const number = Number(raw);
    if (!Number.isFinite(number) || number <= 0) return '暂无数据';
    if (numericUnit !== 'seconds') return '待确认（未提供单位）';
    const seconds = Math.max(1, Math.round(number));
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    if (days) return `${days}天${hours ? `${hours}小时` : ''}`;
    if (hours) return `${hours}小时${minutes ? `${minutes}分钟` : ''}`;
    if (minutes) return `${minutes}分钟${remainder ? `${remainder}秒` : ''}`;
    return `${seconds}秒`;
}
