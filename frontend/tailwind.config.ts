import type { Config } from 'tailwindcss';

const config: Config = {
  // class 策略：ThemeProvider 与 layout 内联脚本通过 <html class="dark"> 切换
  darkMode: 'class',
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      // 语义化颜色变量：亮/暗值在 globals.css 中定义，
      // 用法如 bg-page、text-primary、border-app，暗色自动生效
      colors: {
        page: 'var(--bg-page)',
        surface: 'var(--bg-surface)',
        'text-primary': 'var(--text-primary)',
        'text-secondary': 'var(--text-secondary)',
        'border-app': 'var(--border-app)',
      },
    },
  },
  plugins: [],
};

export default config;
