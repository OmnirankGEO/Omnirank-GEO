"""CSS theme tokens for public report v3."""

from __future__ import annotations


THEMES = {
    "light_corporate": {
        "bg": "#f8fafc",
        "paper": "#ffffff",
        "ink": "#172033",
        "muted": "#64748b",
        "accent": "#0f766e",
        "accent_soft": "#ccfbf1",
        "line": "#dbe4ee",
    },
    "dark_premium": {
        "bg": "#0d1117",
        "paper": "#111827",
        "ink": "#f8fafc",
        "muted": "#a8b3c7",
        "accent": "#f59e0b",
        "accent_soft": "#2b2111",
        "line": "#263244",
    },
    "warm_trust": {
        "bg": "#fbfaf7",
        "paper": "#ffffff",
        "ink": "#232323",
        "muted": "#6b665e",
        "accent": "#0f766e",
        "accent_soft": "#e5f4ee",
        "line": "#e1ded6",
    },
    "bold_gradient": {
        "bg": "#f7f9ff",
        "paper": "#ffffff",
        "ink": "#151a2d",
        "muted": "#5f6680",
        "accent": "#be123c",
        "accent_soft": "#ffe4e6",
        "line": "#d8def0",
    },
}


def get_theme(name: str | None) -> tuple[str, dict]:
    key = name if name in THEMES else "light_corporate"
    return key, THEMES[key]
