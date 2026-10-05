import { createContext, useContext, useState, type ReactNode } from 'react';

export type UserMode = 'geo' | 'social' | 'full';

interface UserModeContextType {
    mode: UserMode;
    setMode: (mode: UserMode) => void;
    hasCompletedOnboarding: boolean;
    completeOnboarding: () => void;
    resetOnboarding: () => void;
}

const STORAGE_KEY = 'omnirank_user_mode';
const ONBOARDING_KEY = 'omnirank_onboarding_done';

const UserModeContext = createContext<UserModeContextType>({
    mode: 'full',
    setMode: () => {},
    hasCompletedOnboarding: false,
    completeOnboarding: () => {},
    resetOnboarding: () => {},
});

export function UserModeProvider({ children }: { children: ReactNode }) {
    const [mode, setModeState] = useState<UserMode>(() => {
        const saved = localStorage.getItem(STORAGE_KEY) as UserMode | null;
        return saved && ['geo', 'social', 'full'].includes(saved) ? saved : 'full';
    });

    const [hasCompletedOnboarding, setHasCompletedOnboarding] = useState<boolean>(
        () => localStorage.getItem(ONBOARDING_KEY) === 'true'
    );

    const setMode = (m: UserMode) => {
        localStorage.setItem(STORAGE_KEY, m);
        setModeState(m);
    };

    const completeOnboarding = () => {
        localStorage.setItem(ONBOARDING_KEY, 'true');
        setHasCompletedOnboarding(true);
    };

    const resetOnboarding = () => {
        localStorage.removeItem(ONBOARDING_KEY);
        setHasCompletedOnboarding(false);
    };

    return (
        <UserModeContext.Provider value={{ mode, setMode, hasCompletedOnboarding, completeOnboarding, resetOnboarding }}>
            {children}
        </UserModeContext.Provider>
    );
}

export function useUserMode(): UserModeContextType {
    return useContext(UserModeContext);
}
