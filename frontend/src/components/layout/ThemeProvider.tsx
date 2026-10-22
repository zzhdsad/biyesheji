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
 * - SSR 水合前按科幻暗色渲染（与默认偏好一致；light/system 用户首帧后校正），
 *   body 背景由 CSS 变量 + 内联脚本保证无白闪
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
    : true; // 默认科幻暗色：SSR 首帧与默认偏好一致，避免暗色用户白闪

  // 同步 Tailwind dark class + 原生 color-scheme
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('dark', isDark);
    root.style.colorScheme = isDark ? 'dark' : 'light';
  }, [isDark]);

  const themeConfig = useMemo(
    () => ({
      algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
      token: {
        colorPrimary: isDark ? '#22d3ee' : '#0891b2',
        colorInfo: isDark ? '#38bdf8' : '#0284c7',
        colorSuccess: '#34d399',
        colorWarning: '#fbbf24',
        colorError: '#fb7185',
        borderRadius: 10,
        fontFamily:
          '"Inter", "HarmonyOS Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
        colorBgLayout: isDark ? '#050b18' : '#eef7ff',
        colorBgContainer: isDark ? '#0a1428' : '#ffffff',
        colorBgElevated: isDark ? '#0d1b33' : '#ffffff',
        colorBorder: isDark ? 'rgba(56, 189, 248, 0.28)' : '#bae6fd',
        colorBorderSecondary: isDark ? 'rgba(56, 189, 248, 0.16)' : '#dbeafe',
        colorText: isDark ? 'rgba(226, 244, 255, 0.94)' : 'rgba(8, 32, 56, 0.9)',
        colorTextSecondary: isDark ? 'rgba(165, 210, 235, 0.72)' : 'rgba(15, 62, 94, 0.68)',
        colorTextTertiary: isDark ? 'rgba(130, 175, 200, 0.55)' : 'rgba(30, 80, 110, 0.48)',
      },
      components: {
        Layout: {
          headerBg: isDark ? 'rgba(5, 11, 24, 0.78)' : 'rgba(255, 255, 255, 0.82)',
          bodyBg: 'transparent',
          siderBg: 'transparent',
        },
      },
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
