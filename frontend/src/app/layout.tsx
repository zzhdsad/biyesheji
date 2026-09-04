import type { Metadata } from 'next';
import { AntdRegistry } from '@ant-design/nextjs-registry';
import { AuthInit } from '@/components/AuthInit';
import { ThemeProvider } from '@/components/layout/ThemeProvider';
import { THEME_STORAGE_KEY } from '@/constants/theme';
import { Providers } from './providers';
import './globals.css';

export const metadata: Metadata = {
  title: '智能知识问答平台',
  description: '基于 RAG 的企业级智能知识问答平台',
};

/**
 * 首帧主题预置脚本：在 React 水合前根据 localStorage 偏好给 <html> 打上
 * dark class，配合 globals.css 的 CSS 变量，避免暗色用户刷新时白屏闪烁。
 * 主题持久化格式与 zustand persist 一致：{"state":{"mode":"dark"},"version":0}
 */
const themeInitScript = `
try {
  var raw = localStorage.getItem('${THEME_STORAGE_KEY}');
  var mode = raw ? (JSON.parse(raw).state || {}).mode : 'system';
  if (mode !== 'light' && mode !== 'dark') mode = 'system';
  var dark = mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var cls = document.documentElement.classList;
  dark ? cls.add('dark') : cls.remove('dark');
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
} catch (e) {}
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // suppressHydrationWarning：内联脚本在水合前修改 html class，抑制必然的属性差异告警
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body>
        <AntdRegistry>
          <ThemeProvider>
            <Providers>
              <AuthInit>{children}</AuthInit>
            </Providers>
          </ThemeProvider>
        </AntdRegistry>
      </body>
    </html>
  );
}
