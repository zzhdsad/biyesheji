'use client';

import { Layout, theme } from 'antd';
import { AppSider } from './AppSider';

const { Content } = Layout;

interface AppLayoutProps {
  /** 头部右侧区域，由具体页面注入（聊天页：知识库选择+新建会话+用户菜单；管理页：标题+按钮等）。 */
  headerLeft?: React.ReactNode;
  headerRight?: React.ReactNode;
  /** 面包屑/页面标题位置文字（管理页使用）；聊天页留白，用 headerLeft/Right 完整控制。 */
  pageTitle?: string;
  children: React.ReactNode;
  /** 内边距：聊天页需要自定义 padding，管理页统一用 24。 */
  contentStyle?: React.CSSProperties;
  /** 聊天页还需 Footer 区域。 */
  footer?: React.ReactNode;
}

/**
 * 全站统一布局：
 * - 左侧 AppSider（导航 + 知识库列表 + 历史会话 + 主题切换）
 * - 顶部 Header（左：pageTitle / headerLeft；右：headerRight）
 * - Content 承载 page 内容，Footer 可选（聊天页输入区）
 *
 * 保证「问答页」和「所有管理页」共享同一套导航与品牌头部，
 * 用户不会在管理页迷路、也能从侧栏一步跳回问答。
 */
export function AppLayout({
  headerLeft,
  headerRight,
  pageTitle,
  children,
  contentStyle,
  footer,
}: AppLayoutProps) {
  const { token } = theme.useToken();

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <AppSider />
      <Layout>
        <Layout.Header
          style={{
            background: token.colorBgContainer,
            borderBottom: `1px solid ${token.colorBorderSecondary}`,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            paddingInline: 24,
            height: 56,
            lineHeight: '56px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 16, minWidth: 0 }}>
            {headerLeft ?? (pageTitle ? <h2 style={{ margin: 0, fontSize: 18 }}>{pageTitle}</h2> : null)}
          </div>
          {headerRight}
        </Layout.Header>

        <Content style={contentStyle ?? { padding: 24, overflow: 'auto' }}>{children}</Content>

        {footer && (
          <Layout.Footer
            style={{
              padding: '12px 24px',
              background: token.colorBgContainer,
              borderTop: `1px solid ${token.colorBorderSecondary}`,
            }}
          >
            {footer}
          </Layout.Footer>
        )}
      </Layout>
    </Layout>
  );
}
