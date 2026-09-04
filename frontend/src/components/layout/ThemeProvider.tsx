'use client';

import { App, ConfigProvider, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { resolveTheme, useThemeStore } from '@/stores/themeStore';

/**
 * 主题 Provider：antd 5 ConfigProvider 动态算法 + Tailwind dark class 同步。
 *
 * - 从 themeStore 读用户偏好（light/dark/system，localStorage 持久化）
 * - 'system' 模式监听 prefers-color-scheme 变化实时跟随
 * - 同步 <html class="dark"> 给 Tailwind（darkMode: 'class'）+ color-scheme
 *   给浏览器原生控件（滚动条/输入框自动填充等）
 * - SSR 水合前按亮色渲染（与内联预置脚本设置的 html.dark 并存：antd 首帧
 *   可能短暂亮色，body 背景由 CSS 变量 + 内联脚本保证无白闪）
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const mode = useThemeStore((s) => s.mode);

  // 水合防护：mounted 前按亮色渲染，避免 localStorage 持久化值导致的 SSR 不匹配
  const [mounted, setMounted] = useState(false);
  const [systemDark, setSystemDark] = useState(false);

  useEffect(() => {
    setMounted(true);
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    setSystemDark(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const isDark = mounted
    ? mode === 'dark' || (mode === 'system' && systemDark)
    : false;

  // 同步 Tailwind dark class + 原生 color-scheme
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('dark', isDark);
    root.style.colorScheme = isDark ? 'dark' : 'light';
  }, [isDark]);

  const themeConfig = useMemo(
    () => ({
      algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
      token: { colorPrimary: '#1677ff', borderRadius: 8 },
    }),
    [isDark],
  );

  return (
    <ConfigProvider locale={zhCN} theme={themeConfig}>
      {/* antd App 提供 message/notification/modal 的上下文式 API（App.useApp） */}
      <App>{children}</App>
    </ConfigProvider>
  );
}
