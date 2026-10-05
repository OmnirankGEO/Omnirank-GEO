/**
 * 新手引导上下文
 * 管理引导状态：欢迎选择、步骤完成、功能气泡关闭、视频已看
 * 持久化到 localStorage，并兼容老 key 自动迁移为"老用户"(welcome_choice='never')
 */
import { createContext, useContext, useState, useEffect, useCallback, useRef, ReactNode } from 'react';

const STORAGE_KEY = 'omnirank_onboarding_state';
// 老 key 列表：任一存在 → 视为老用户，跳过欢迎弹窗
const LEGACY_KEYS = [
    'omnirank_m3_onboarded_at',
    'omnirank_m3_onboarding_skipped',
    'onboarding_state',
] as const;
const SCHEMA_VERSION = 1;

export interface OnboardingState {
    version: number;
    welcome_choice?: 'start' | 'later' | 'never';
    completed_steps: string[];
    /** 用户主动跳过的 step_id 列表 (Stage 1 Batch 1, 2026-05-06) */
    skipped_steps: string[];
    dismissed_features: string[];
    viewed_videos: string[];
    first_seen_at: string;
    last_updated_at: string;
}

function makeInitialState(): OnboardingState {
    const now = new Date().toISOString();
    return {
        version: SCHEMA_VERSION,
        completed_steps: [],
        skipped_steps: [],
        dismissed_features: [],
        viewed_videos: [],
        first_seen_at: now,
        last_updated_at: now,
    };
}

export interface OnboardingContextValue {
    state: OnboardingState;
    setWelcomeChoice: (choice: 'start' | 'later' | 'never') => void;
    markStepCompleted: (stepId: string) => void;
    isStepCompleted: (stepId: string) => boolean;
    skipStep: (stepId: string) => void;
    isStepSkipped: (stepId: string) => boolean;
    dismissFeatureTooltip: (featureId: string) => void;
    isFeatureDismissed: (featureId: string) => boolean;
    markVideoViewed: (videoCode: string) => void;
    isVideoViewed: (videoCode: string) => boolean;
    isAvailable: boolean;
}

const OnboardingContext = createContext<OnboardingContextValue | null>(null);

export function useOnboarding(): OnboardingContextValue {
    const ctx = useContext(OnboardingContext);
    if (!ctx) throw new Error('useOnboarding must be used within OnboardingProvider');
    return ctx;
}

/** 检测 localStorage 是否可用（隐私模式 / 配额满 / 被禁用都会抛错） */
function isLocalStorageAvailable(): boolean {
    try {
        const k = '__test_ls__';
        localStorage.setItem(k, '1');
        localStorage.removeItem(k);
        return true;
    } catch {
        return false;
    }
}

/**
 * 从 localStorage 还原状态
 * - 新 key 存在且 schema 匹配 → 直接用
 * - schema 不匹配 → 警告并重置
 * - 任一老 key 存在 → 视为老用户，welcome_choice='never'
 */
function hydrateFromStorage(): { state: OnboardingState; isLegacyUser: boolean } {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (raw) {
            const parsed = JSON.parse(raw) as Partial<OnboardingState>;
            if (parsed.version === SCHEMA_VERSION && Array.isArray(parsed.completed_steps)) {
                // skipped_steps 是 Batch 1 后加的字段, 老存档可能没有 → 兜底空数组
                const hydrated: OnboardingState = {
                    ...(parsed as OnboardingState),
                    skipped_steps: Array.isArray(parsed.skipped_steps) ? parsed.skipped_steps : [],
                    dismissed_features: Array.isArray(parsed.dismissed_features) ? parsed.dismissed_features : [],
                    viewed_videos: Array.isArray(parsed.viewed_videos) ? parsed.viewed_videos : [],
                };
                return { state: hydrated, isLegacyUser: false };
            }
            console.warn('[Onboarding] state schema mismatch, resetting');
        }
    } catch (e) {
        console.warn('[Onboarding] hydrate failed:', e);
    }
    // 检查老 key：任一存在都视为老用户（跳过欢迎弹窗）
    const isLegacyUser = LEGACY_KEYS.some(k => {
        try { return localStorage.getItem(k) !== null; } catch { return false; }
    });
    if (isLegacyUser) {
        return {
            state: { ...makeInitialState(), welcome_choice: 'never' },
            isLegacyUser: true,
        };
    }
    return { state: makeInitialState(), isLegacyUser: false };
}

export function OnboardingProvider({ children }: { children: ReactNode }) {
    const [isAvailable] = useState(isLocalStorageAvailable);
    const [state, setState] = useState<OnboardingState>(() => {
        if (!isAvailable) return makeInitialState();
        return hydrateFromStorage().state;
    });

    const hasMountedRef = useRef(false);

    // 状态变化即落盘（更新 last_updated_at）
    useEffect(() => {
        if (!isAvailable) return;
        // 跳过首次挂载,避免给"未交互访客"产生持久化记录
        if (!hasMountedRef.current) {
            hasMountedRef.current = true;
            return;
        }
        try {
            localStorage.setItem(
                STORAGE_KEY,
                JSON.stringify({ ...state, last_updated_at: new Date().toISOString() }),
            );
        } catch (e) {
            console.warn('[Onboarding] persist failed:', e);
        }
    }, [state, isAvailable]);

    // 同步落盘 helper · 修复"navigate 触发路由切换前 useEffect persist 没跑"导致状态丢失的 bug
    // 调用方:状态更新前的最新 state · 调用后立即写 localStorage, 不依赖 useEffect 时序
    const persistNow = useCallback((next: OnboardingState) => {
        if (!isAvailable) return;
        try {
            localStorage.setItem(
                STORAGE_KEY,
                JSON.stringify({ ...next, last_updated_at: new Date().toISOString() }),
            );
        } catch (e) {
            console.warn('[Onboarding] persistNow failed:', e);
        }
    }, [isAvailable]);

    const setWelcomeChoice = useCallback((choice: 'start' | 'later' | 'never') => {
        setState(s => {
            const next = { ...s, welcome_choice: choice };
            persistNow(next);
            return next;
        });
    }, [persistNow]);

    const markStepCompleted = useCallback((stepId: string) => {
        setState(s => {
            if (s.completed_steps.includes(stepId)) return s;
            const next = { ...s, completed_steps: [...s.completed_steps, stepId] };
            persistNow(next);  // 立刻落盘 · 防 navigate 切路由时丢失
            return next;
        });
    }, [persistNow]);

    const isStepCompleted = useCallback(
        (stepId: string) => state.completed_steps.includes(stepId),
        [state.completed_steps],
    );

    const skipStep = useCallback((stepId: string) => {
        setState(s => {
            if (s.skipped_steps.includes(stepId)) return s;
            const next = { ...s, skipped_steps: [...s.skipped_steps, stepId] };
            persistNow(next);
            return next;
        });
    }, [persistNow]);

    const isStepSkipped = useCallback(
        (stepId: string) => state.skipped_steps.includes(stepId),
        [state.skipped_steps],
    );

    const dismissFeatureTooltip = useCallback((featureId: string) => {
        setState(s => {
            if (s.dismissed_features.includes(featureId)) return s;
            const next = { ...s, dismissed_features: [...s.dismissed_features, featureId] };
            persistNow(next);
            return next;
        });
    }, [persistNow]);

    const isFeatureDismissed = useCallback(
        (featureId: string) => state.dismissed_features.includes(featureId),
        [state.dismissed_features],
    );

    const markVideoViewed = useCallback((videoCode: string) => {
        setState(s => {
            if (s.viewed_videos.includes(videoCode)) return s;
            const next = { ...s, viewed_videos: [...s.viewed_videos, videoCode] };
            persistNow(next);
            return next;
        });
    }, [persistNow]);

    const isVideoViewed = useCallback(
        (videoCode: string) => state.viewed_videos.includes(videoCode),
        [state.viewed_videos],
    );

    const value: OnboardingContextValue = {
        state,
        setWelcomeChoice,
        markStepCompleted,
        isStepCompleted,
        skipStep,
        isStepSkipped,
        dismissFeatureTooltip,
        isFeatureDismissed,
        markVideoViewed,
        isVideoViewed,
        isAvailable,
    };

    return <OnboardingContext.Provider value={value}>{children}</OnboardingContext.Provider>;
}
