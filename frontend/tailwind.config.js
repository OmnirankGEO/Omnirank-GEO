/** @type {import('tailwindcss').Config} */
export default {
    darkMode: ["class"],
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        container: {
            center: true,
            padding: "2rem",
            screens: {
                "2xl": "1400px",
            },
        },
        // E.6 (CTO-15.18 · 2026-04-28):响应式 breakpoint 三档(Q20 老板裁决)
        // sm < 640 移动 / md 640-1023 平板单列 / lg ≥1024 桌面 2 栏 / xl ≥1280 桌面 2 栏 + AI 副驾右栏 / 2xl ≥1536 桌面 3 栏
        screens: {
            sm: '640px',
            md: '768px',
            lg: '1024px',
            xl: '1280px',
            '2xl': '1536px',
        },
        extend: {
            // E.7 (CTO-15.18 · 2026-04-28):PC 密度令牌(spacing / fontSize 扩展)
            // PC padding 16-20 / 字号 14-15 / 行高 1.4-1.5 · CSS 变量 --pc-* 在 styles/density.css
            spacing: {
                'pc-xs': '4px',
                'pc-sm': '8px',
                'pc-md': '12px',
                'pc-lg': '16px',
                'pc-xl': '20px',
                'pc-2xl': '24px',
            },
            fontSize: {
                'pc-xs': ['12px', { lineHeight: '1.4' }],
                'pc-sm': ['13px', { lineHeight: '1.4' }],
                'pc-base': ['14px', { lineHeight: '1.5' }],
                'pc-md': ['15px', { lineHeight: '1.5' }],
                'pc-lg': ['16px', { lineHeight: '1.5' }],
            },
            fontFamily: {
                sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
            },
            colors: {
                border: "hsl(var(--border))",
                input: "hsl(var(--input))",
                ring: "hsl(var(--ring))",
                background: "hsl(var(--background))",
                foreground: "hsl(var(--foreground))",
                primary: {
                    DEFAULT: "hsl(var(--primary))",
                    foreground: "hsl(var(--primary-foreground))",
                },
                secondary: {
                    DEFAULT: "hsl(var(--secondary))",
                    foreground: "hsl(var(--secondary-foreground))",
                },
                destructive: {
                    DEFAULT: "hsl(var(--destructive))",
                    foreground: "hsl(var(--destructive-foreground))",
                },
                muted: {
                    DEFAULT: "hsl(var(--muted))",
                    foreground: "hsl(var(--muted-foreground))",
                },
                accent: {
                    DEFAULT: "hsl(var(--accent))",
                    foreground: "hsl(var(--accent-foreground))",
                },
                popover: {
                    DEFAULT: "hsl(var(--popover))",
                    foreground: "hsl(var(--popover-foreground))",
                },
                card: {
                    DEFAULT: "hsl(var(--card))",
                    foreground: "hsl(var(--card-foreground))",
                },
                brand: {
                    DEFAULT: "hsl(var(--brand))",
                    hover: "hsl(var(--brand-hover))",
                    light: "hsl(var(--brand-light))",
                    dark: "hsl(var(--brand-dark))",
                },
                sidebar: {
                    DEFAULT: "hsl(var(--sidebar))",
                    foreground: "hsl(var(--sidebar-foreground))",
                    active: "hsl(var(--sidebar-active))",
                    "active-foreground": "hsl(var(--sidebar-active-foreground))",
                    border: "hsl(var(--sidebar-border))",
                },
            },
            borderRadius: {
                lg: "var(--radius)",
                md: "calc(var(--radius) - 2px)",
                sm: "calc(var(--radius) - 4px)",
            },
            keyframes: {
                "accordion-down": {
                    from: { height: "0" },
                    to: { height: "var(--radix-accordion-content-height)" },
                },
                "accordion-up": {
                    from: { height: "var(--radix-accordion-content-height)" },
                    to: { height: "0" },
                },
            },
            animation: {
                "accordion-down": "accordion-down 0.2s ease-out",
                "accordion-up": "accordion-up 0.2s ease-out",
            },
        },
    },
    plugins: [require("tailwindcss-animate"), require("@tailwindcss/typography")],
}
