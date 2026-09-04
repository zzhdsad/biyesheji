'use client';

import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';
import { THEME_STORAGE_KEY } from '@/constants/theme';

/** 主题模式：亮色 / 暗色 / 跟随系统（PRD §5.3 明亮-暗色双主题）。 */
export type ThemeMode = 'light' | 'dark' | 'system';

interface ThemeState {
  mode: ThemeMode;
  /** 切换主题模式（持久化到 localStorage，key: kp-theme）。 */
  setMode: (mode: ThemeMode) => void;
}

export const useThemeStore = create<ThemeState>()(
  persist(
    (set) => ({
      mode: 'system',
      setMode: (mode) => set({ mode }),
    }),
    {
      name: THEME_STORAGE_KEY,
      storage: createJSONStorage(() => localStorage),
    },
  ),
);

/**
 * 解析最终生效主题：'system' 时读系统 prefers-color-scheme。
 * SSR 安全：服务端无 window，一律返回 light（首帧后由 ThemeProvider 校正）。
 */
export function resolveTheme(mode: ThemeMode): 'light' | 'dark' {
  if (mode === 'system') {
    if (
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches
    ) {
      return 'dark';
    }
    return 'light';
  }
  return mode;
}
