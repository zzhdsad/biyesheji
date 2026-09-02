import type { Metadata } from 'next';
import { App, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { AntdRegistry } from '@ant-design/nextjs-registry';
import { AuthInit } from '@/components/AuthInit';
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
            {/* antd App 提供 message/notification/modal 的上下文式 API（App.useApp） */}
            <App>
              <Providers>
                <AuthInit>{children}</AuthInit>
              </Providers>
            </App>
          </ConfigProvider>
        </AntdRegistry>
      </body>
    </html>
  );
}
