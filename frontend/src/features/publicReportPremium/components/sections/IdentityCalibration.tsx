/**
 * 待确认校准 · **服务商视角专属**(WO 2026-08-07 · Owner 拍板)
 *
 * 🔴 渲染条件 = `report.calibration` 在不在,**不判身份**。
 *    服务端已按登录态 + 资源级归属裁剪过:匿名访客的响应体里根本没有这个键。
 *    前端再判一次 = 第二处口径,两处一旦不一致就是漏洞。
 *
 * 🔴 **软引导,不是硬门**:提示条只建议"校准后再发给客户",
 *    不阻断分享、不弹确认框(顶层铁律:不新增自证/阻断环节)。
 *
 * 确认动作调**现役服务商端那条端点**(与工作台同一条后端路径,不开第二条写路径),
 * 成功后整页重载 —— 分享页每次请求读库,所以刷新即见新数字。
 */
import { useState } from 'react';

import type { IdentityCalibration as CalibrationData, IdentityCalibrationItem } from '../../contract/types';

interface Props {
    readonly calibration: CalibrationData;
    readonly reportId: string;
    readonly shareToken?: string | null;
}

/** 与工作台同源的 request_id 生成(端点要求 UUID) */
function newRequestId(): string {
    const c = globalThis.crypto as Crypto | undefined;
    if (c && typeof c.randomUUID === 'function') return c.randomUUID();
    // 兜底:老 Safari / 非安全上下文没有 randomUUID
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (ch) => {
        const r = (Math.random() * 16) | 0;
        const v = ch === 'x' ? r : (r & 0x3) | 0x8;
        return v.toString(16);
    });
}

export function IdentityCalibration({ calibration, reportId, shareToken }: Props) {
    const [busyKey, setBusyKey] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    if (!calibration || calibration.pendingCount <= 0 || calibration.items.length === 0) {
        return null;
    }

    const decide = async (item: IdentityCalibrationItem, isSameCompany: boolean) => {
        const key = `${item.question}::${item.engine}`;
        setBusyKey(key);
        setError(null);
        try {
            const url = calibration.decisionEndpoint.replace('{diagnosis_id}', reportId);
            const response = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    question: item.question,
                    engine: item.engine,
                    action: isSameCompany ? 'confirm_yes' : 'confirm_no',
                    selected_name: item.similarNames[0] ?? undefined,
                    expected_version: item.decisionVersion,
                    request_id: newRequestId(),
                }),
            });
            if (!response.ok) {
                setError('这一条没保存成功，请稍后再试。');
                setBusyKey(null);
                return;
            }
            // 确认后数字要变:分享页每次请求读库,重载即见新的出现率与计数。
            const next = new URL(window.location.href);
            if (shareToken) next.searchParams.set('st', shareToken);
            window.location.replace(next.toString());
        } catch {
            setError('这一条没保存成功，请稍后再试。');
            setBusyKey(null);
        }
    };

    return (
        <section
            data-testid="identity-calibration"
            className="mx-auto mb-6 w-full max-w-[1320px] px-4 sm:px-6 lg:px-10"
        >
            <div className="rounded-xl border border-amber-300 bg-amber-50 p-4">
                {/* 软提示条:建议,不阻断 */}
                <p
                    data-testid="identity-calibration-banner"
                    className="text-sm font-medium text-amber-900"
                >
                    还有 {calibration.pendingCount} 项待确认，建议校准后再发给客户
                </p>
                <p className="mt-1 text-xs text-amber-800/80">
                    这几次回答里出现了和你品牌相近的名字。确认是同一家之后，这些结果会计入统计。
                </p>

                <ul className="mt-3 space-y-2">
                    {calibration.items.map((item) => {
                        const key = `${item.question}::${item.engine}`;
                        const busy = busyKey === key;
                        return (
                            <li
                                key={key}
                                data-testid="identity-calibration-item"
                                className="rounded-lg border border-amber-200 bg-white p-3"
                            >
                                <p className="text-xs text-slate-500">
                                    {item.platformName} · {item.question}
                                </p>
                                {item.similarNames.length > 0 && (
                                    <p className="mt-1 text-sm text-slate-800">
                                        回答里出现的相近名字：
                                        <span className="font-semibold">
                                            {item.similarNames.join('、')}
                                        </span>
                                    </p>
                                )}
                                {item.answerExcerpt && (
                                    <p className="mt-1 line-clamp-2 text-xs leading-5 text-slate-500">
                                        {item.answerExcerpt}
                                    </p>
                                )}
                                <div className="mt-2 flex flex-wrap gap-2">
                                    <button
                                        type="button"
                                        data-testid="identity-calibration-same"
                                        disabled={busy}
                                        onClick={() => void decide(item, true)}
                                        className="min-h-9 rounded-md bg-amber-600 px-3 text-xs font-medium text-white disabled:opacity-60"
                                    >
                                        是同一家
                                    </button>
                                    <button
                                        type="button"
                                        data-testid="identity-calibration-different"
                                        disabled={busy}
                                        onClick={() => void decide(item, false)}
                                        className="min-h-9 rounded-md border border-slate-300 bg-white px-3 text-xs font-medium text-slate-700 disabled:opacity-60"
                                    >
                                        不是
                                    </button>
                                </div>
                            </li>
                        );
                    })}
                </ul>

                {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
            </div>
        </section>
    );
}

export default IdentityCalibration;
