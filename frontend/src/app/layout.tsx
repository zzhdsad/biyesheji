import type { Metadata } from 'next';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { AntdRegistry } from '@ant-design/nextjs-registry';
import { Providers } from './providers';
import './globals.css';

export const metadata: Metadata = {
  title: '智能知识问答平台',
  description: '基于 RAG 的企业级智能知识问答平台',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <AntdRegistry>
          <ConfigProvider
            locale={zhCN}
            theme={{ token: { colorPrimary: '#1677ff', borderRadius: 8 } }}
          >
            <Providers>{children}</Providers>
          </ConfigProvider>
        </AntdRegistry>
      </body>
    </html>
  );
}
