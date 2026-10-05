import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

type Theme = "light" | "dark" | "system";

interface ThemeContextType {
    theme: Theme;
    setTheme: (theme: Theme) => void;
    resolvedTheme: "light" | "dark";
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

const STORAGE_KEY = "omnirank-theme";

function getSystemTheme(): "light" | "dark" {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// Phase 08.3 (CTO-15.23 2026-05-04) · P0 · iOS Safari 强隐私模式 localStorage 整个 API throws
// 不 try/catch → ThemeProvider useState initializer 崩 → React boot 失败 → 白屏
function safeGetItem(key: string): string | null {
    try { return localStorage.getItem(key); } catch { return null; }
}
function safeSetItem(key: string, value: string): void {
    try { localStorage.setItem(key, value); } catch { /* 隐私模式静默 */ }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
    const [theme, setThemeState] = useState<Theme>(() => {
        const stored = safeGetItem(STORAGE_KEY);
        return (stored as Theme) || "dark";
    });

    const resolvedTheme = theme === "system" ? getSystemTheme() : theme;

    useEffect(() => {
        const root = document.documentElement;
        root.classList.remove("light", "dark");
        root.classList.add(resolvedTheme);
    }, [resolvedTheme]);

    // 监听系统主题变化（当选择 system 时）
    useEffect(() => {
        if (theme !== "system") return;
        const mq = window.matchMedia("(prefers-color-scheme: dark)");
        const handler = () => {
            const root = document.documentElement;
            root.classList.remove("light", "dark");
            root.classList.add(getSystemTheme());
        };
        mq.addEventListener("change", handler);
        return () => mq.removeEventListener("change", handler);
    }, [theme]);

    const setTheme = (t: Theme) => {
        setThemeState(t);
        safeSetItem(STORAGE_KEY, t);
    };

    return (
        <ThemeContext.Provider value={{ theme, setTheme, resolvedTheme }}>
            {children}
        </ThemeContext.Provider>
    );
}

export function useTheme() {
    const ctx = useContext(ThemeContext);
    if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
    return ctx;
}
