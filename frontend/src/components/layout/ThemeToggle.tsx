import { Sun, Moon, Monitor } from "lucide-react";
import { useTheme } from "@/context/ThemeContext";
import { cn } from "@/lib/utils";

interface ThemeToggleProps {
    collapsed?: boolean;
}

export function ThemeToggle({ collapsed = false }: ThemeToggleProps) {
    const { theme, setTheme } = useTheme();

    // 折叠模式：单按钮切换
    if (collapsed) {
        const next = theme === "light" ? "dark" : theme === "dark" ? "system" : "light";
        const Icon = theme === "dark" ? Moon : theme === "system" ? Monitor : Sun;
        return (
            <button
                onClick={() => setTheme(next)}
                className="p-2.5 rounded-lg text-muted-foreground hover:bg-sidebar-active hover:text-foreground transition-colors"
                title={`主题: ${theme === "light" ? "浅色" : theme === "dark" ? "深色" : "跟随系统"}`}
            >
                <Icon className="h-5 w-5" />
            </button>
        );
    }

    // 展开模式：三选一图标切换条
    return (
        <div className="flex items-center gap-1 bg-sidebar-active rounded-lg p-0.5">
            <button
                onClick={() => setTheme("light")}
                className={cn(
                    "flex items-center justify-center p-1.5 rounded-md transition-colors",
                    theme === "light"
                        ? "bg-card text-foreground shadow-xs"
                        : "text-muted-foreground hover:text-foreground"
                )}
                title="浅色模式"
            >
                <Sun className="h-4 w-4" />
            </button>
            <button
                onClick={() => setTheme("dark")}
                className={cn(
                    "flex items-center justify-center p-1.5 rounded-md transition-colors",
                    theme === "dark"
                        ? "bg-card text-foreground shadow-xs"
                        : "text-muted-foreground hover:text-foreground"
                )}
                title="深色模式"
            >
                <Moon className="h-4 w-4" />
            </button>
            <button
                onClick={() => setTheme("system")}
                className={cn(
                    "flex items-center justify-center p-1.5 rounded-md transition-colors",
                    theme === "system"
                        ? "bg-card text-foreground shadow-xs"
                        : "text-muted-foreground hover:text-foreground"
                )}
                title="跟随系统"
            >
                <Monitor className="h-4 w-4" />
            </button>
        </div>
    );
}
