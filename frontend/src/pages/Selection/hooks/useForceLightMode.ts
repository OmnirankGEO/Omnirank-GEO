import { useLayoutEffect } from 'react';

/**
 * 公开客户页面强制使用浅色模式。
 * 挂载时移除 dark 类并加 light，卸载时恢复原始主题类。
 */
export function useForceLightMode() {
  useLayoutEffect(() => {
    const root = document.documentElement;
    const body = document.body;
    const wasDark = root.classList.contains('dark');
    const hadLight = root.classList.contains('light');
    const previousRootColorScheme = root.style.colorScheme;
    const previousBodyColorScheme = body.style.colorScheme;
    const previousBodyBackground = body.style.backgroundColor;
    const previousBodyColor = body.style.color;
    const previousMeta = document.querySelector('meta[name="color-scheme"]') as HTMLMetaElement | null;
    const previousMetaContent = previousMeta?.getAttribute('content') ?? null;
    const colorSchemeMeta = previousMeta ?? document.createElement('meta');

    if (!previousMeta) {
      colorSchemeMeta.setAttribute('name', 'color-scheme');
      document.head.appendChild(colorSchemeMeta);
    }
    colorSchemeMeta.setAttribute('content', 'light');

    root.classList.remove('dark');
    root.classList.add('light');
    root.style.colorScheme = 'light';
    body.style.colorScheme = 'light';
    body.style.backgroundColor = '#f8fafc';
    body.style.color = '#0f172a';

    return () => {
      if (wasDark) {
        root.classList.add('dark');
      }
      if (!hadLight) {
        root.classList.remove('light');
      }
      root.style.colorScheme = previousRootColorScheme;
      body.style.colorScheme = previousBodyColorScheme;
      body.style.backgroundColor = previousBodyBackground;
      body.style.color = previousBodyColor;
      if (previousMeta) {
        previousMetaContent === null
          ? previousMeta.removeAttribute('content')
          : previousMeta.setAttribute('content', previousMetaContent);
      } else {
        colorSchemeMeta.remove();
      }
    };
  }, []);
}
