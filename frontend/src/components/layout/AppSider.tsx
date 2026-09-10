'use client';

import { useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import {
  App,
  Avatar,
  Badge,
  Button,
  Divider,
  Dropdown,
  Layout,
  List,
  Menu,
  Popconfirm,
  Spin,
  Tooltip,
  Typography,
  theme,
} from 'antd';
import type { MenuProps } from 'antd';
import {
  AppstoreOutlined,
  BulbFilled,
  BulbOutlined,
  ControlOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  DesktopOutlined,
  DeleteOutlined,
  DownOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  HistoryOutlined,
  LogoutOutlined,
  MessageOutlined,
  PlusOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useChatStore } from '@/stores/chatStore';
import { useThemeStore, type ThemeMode } from '@/stores/themeStore';
import { useUserStore } from '@/stores/userStore';

const { Sider } = Layout;

/** 主题下拉的选项文案。 */
const THEME_LABELS: Record<ThemeMode, string> = {
  light: '亮色模式',
  dark: '暗色模式',
  system: '跟随系统',
};

/** 顶部主导航：问答 + 管理中心。key 即路由路径。 */
const NAV_ITEMS: Required<MenuProps>['items'] = [
  {
    key: 'chat',
    icon: <MessageOutlined />,
    label: '智能问答',
    onClick: () => window.location.assign('/chat'),
  },
  {
    key: 'admin-group',
    label: (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        <SettingOutlined /> 管理中心
      </span>
    ),
    type: 'group',
    children: [
      { key: '/kb', icon: <DatabaseOutlined />, label: '知识库管理' },
      { key: '/documents', icon: <FileTextOutlined />, label: '文档管理' },
      { key: '/users', icon: <TeamOutlined />, label: '用户管理' },
      { key: '/evaluation', icon: <ExperimentOutlined />, label: '评估面板' },
      { key: '/admin', icon: <DashboardOutlined />, label: '系统仪表盘' },
      { key: '/settings', icon: <ControlOutlined />, label: '模型设置' },
    ],
  },
];

/** 右上角系统状态指示器：postgres/redis/milvus 全 ok 则绿，否则红。 */
function SystemStatus() {
  const { health } = useChatStore();
  const comps = health?.components ?? {};
  const allOk =
    Object.keys(comps).length > 0 && Object.values(comps).every((v) => v === 'ok');
  const detail = Object.entries(comps)
    .map(([k, v]) => `${k}: ${v}`)
    .join('，');
  return (
    <Tooltip title={detail || '检查中…'}>
      <Badge
        status={allOk ? 'success' : 'error'}
        text={
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            {allOk ? '系统正常' : '系统异常'}
          </Typography.Text>
        }
      />
    </Tooltip>
  );
}

/** 右上角用户菜单：头像 + 用户名，下拉登出。 */
function UserMenu() {
  const { user, logout } = useUserStore();
  if (!user) return null;
  const items: MenuProps['items'] = [
    {
      key: 'info',
      label: (
        <div style={{ padding: '4px 0' }}>
          <Typography.Text strong>{user.username}</Typography.Text>
          <br />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {user.email}
          </Typography.Text>
        </div>
      ),
      disabled: true,
    },
    { type: 'divider' },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      label: '退出登录',
      onClick: () => void logout(),
    },
  ];
  return (
    <Dropdown menu={{ items }} placement="bottomRight">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
        <Avatar size="small" icon={<UserOutlined />} />
        <Typography.Text>{user.username}</Typography.Text>
      </div>
    </Dropdown>
  );
}

/**
 * 左侧边栏（全站共享）：
 * 1. 品牌 Logo 区域
 * 2. 顶部主导航：智能问答 / 管理中心（知识库/文档/评估/仪表盘）
 * 3. 历史会话列表（+ 单条删除 + 全部删除）
 * 4. 底部：刷新会话 / 主题切换
 */
export function AppSider() {
  const {
    conversations,
    currentConversationId,
    selectConversation,
    deleteConversation,
    deleteAllConversations,
    newConversation,
    loadingConversations,
    loadConversations,
    loadHealth,
  } = useChatStore();

  // 全局加载系统健康状态（所有页面共享 SystemStatus 组件）
  useEffect(() => {
    void loadHealth();
    const t = setInterval(() => void loadHealth(), 30_000);
    return () => clearInterval(t);
  }, [loadHealth]);

  const themeMode = useThemeStore((s) => s.mode);
  const setThemeMode = useThemeStore((s) => s.setMode);
  const { message } = App.useApp();
  const router = useRouter();
  const pathname = usePathname();

  // token 化配色：跟随 ConfigProvider 算法自动亮/暗切换（不再硬编码浅色值）
  const { token } = theme.useToken();

  const themeMenuItems: MenuProps['items'] = [
    { key: 'light', icon: <BulbOutlined />, label: THEME_LABELS.light },
    { key: 'dark', icon: <BulbFilled />, label: THEME_LABELS.dark },
    { key: 'system', icon: <DesktopOutlined />, label: THEME_LABELS.system },
  ];

  const onThemeMenuClick: MenuProps['onClick'] = ({ key }) => {
    setThemeMode(key as ThemeMode);
    message.success(`已切换为${THEME_LABELS[key as ThemeMode]}`);
  };

  // 主导航点击（管理中心子项）→ 跳转对应路由
  const onNavClick: MenuProps['onClick'] = ({ key }) => {
    if (key.startsWith('/')) {
      router.push(key);
    }
  };

  // 当前在哪个管理页？（用于高亮主导航的选中子项）
  const selectedNavKeys: string[] = (() => {
    if (pathname.startsWith('/kb')) return ['/kb'];
    if (pathname.startsWith('/documents')) return ['/documents'];
    if (pathname.startsWith('/users')) return ['/users'];
    if (pathname.startsWith('/evaluation')) return ['/evaluation'];
    if (pathname.startsWith('/admin')) return ['/admin'];
    if (pathname.startsWith('/settings')) return ['/settings'];
    return ['chat'];
  })();

  return (
    <Sider
      width={272}
      style={{
        background: token.colorBgContainer,
        borderRight: `1px solid ${token.colorBorderSecondary}`,
        padding: 16,
        overflow: 'auto',
        height: '100vh',
        position: 'sticky',
        top: 0,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* 品牌区 */}
      <div
        onClick={() => router.push('/chat')}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '4px 8px 12px',
          cursor: 'pointer',
        }}
      >
        <AppstoreOutlined
          style={{
            fontSize: 22,
            color: token.colorPrimary,
          }}
        />
        <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.2 }}>
          <Typography.Title level={5} style={{ margin: 0 }}>
            知识问答平台
          </Typography.Title>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            RAG · 智能检索
          </Typography.Text>
        </div>
      </div>

      {/* 主导航：问答 + 管理中心 */}
      <Menu
        mode="inline"
        selectedKeys={selectedNavKeys}
        items={NAV_ITEMS}
        onClick={onNavClick}
        style={{
          background: 'transparent',
          borderInlineEnd: 'none',
          marginBottom: 8,
        }}
      />

      <Button
        type="dashed"
        icon={<PlusOutlined />}
        block
        onClick={() => newConversation()}
        style={{ marginTop: 4 }}
      >
        新建会话
      </Button>

      <Divider orientation="left" plain style={{ marginTop: 12 }}>
        <HistoryOutlined /> 历史会话
        {conversations.length > 0 && (
          <Popconfirm
            title="删除全部历史会话？"
            description="所有会话及其消息将被永久删除，不可恢复"
            okText="全部删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => void deleteAllConversations()}
          >
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined />}
              style={{ padding: '0 4px', marginLeft: 4 }}
            >
              全部删除
            </Button>
          </Popconfirm>
        )}
      </Divider>
      <Spin spinning={loadingConversations} size="small">
        <List
          size="small"
          dataSource={conversations}
          locale={{ emptyText: '暂无历史会话' }}
          renderItem={(conv) => {
            const active = conv.id === currentConversationId;
            const onClick = () => {
              // 非聊天页时先跳 /chat 再选会话（避免在管理页选了却没跳页）
              if (pathname !== '/chat') {
                router.push('/chat');
                setTimeout(() => void selectConversation(conv.id), 30);
              } else {
                void selectConversation(conv.id);
              }
            };
            return (
              <List.Item
                className="group"
                style={{
                  cursor: 'pointer',
                  background: active ? token.colorPrimaryBg : 'transparent',
                  borderRadius: token.borderRadiusSM,
                  paddingInline: 8,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                }}
                onClick={onClick}
              >
                <Typography.Text
                  ellipsis
                  style={{ flex: 1, minWidth: 0, fontWeight: active ? 600 : 400 }}
                >
                  {conv.title}
                </Typography.Text>
                <Popconfirm
                  title="删除该会话？"
                  description="会话中的所有消息将一并删除"
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                  onConfirm={(e) => {
                    e?.stopPropagation();
                    void deleteConversation(conv.id);
                  }}
                  onCancel={(e) => e?.stopPropagation()}
                >
                  <Tooltip title="删除会话">
                    <Button
                      type="text"
                      size="small"
                      icon={<DeleteOutlined />}
                      onClick={(e) => e.stopPropagation()}
                      className={`opacity-0 group-hover:opacity-100${active ? ' !opacity-100' : ''}`}
                      style={{
                        flex: 'none',
                        color: token.colorTextTertiary,
                      }}
                    />
                  </Tooltip>
                </Popconfirm>
              </List.Item>
            );
          }}
        />
      </Spin>

      {/* 弹性占位：把底部栏压到底部 */}
      <div style={{ flex: 1 }} />

      <div
        style={{
          marginTop: 12,
          paddingTop: 12,
          borderTop: `1px solid ${token.colorBorderSecondary}`,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <Button type="link" size="small" onClick={() => loadConversations()} style={{ padding: 0 }}>
          刷新会话列表
        </Button>

        {/* 主题切换下拉（PRD §5.3：亮色 / 暗色 / 跟随系统） */}
        <Dropdown
          menu={{
            items: themeMenuItems,
            onClick: onThemeMenuClick,
            selectable: true,
            selectedKeys: [themeMode],
          }}
          trigger={['click']}
          placement="topRight"
        >
          <Button size="small" type="text" aria-label="切换主题">
            {themeMode === 'dark' ? <BulbFilled /> : <BulbOutlined />}
            {THEME_LABELS[themeMode]}
            <DownOutlined style={{ fontSize: 10 }} />
          </Button>
        </Dropdown>
      </div>
    </Sider>
  );
}

/** 统一右侧栏 Header 内容（全站通用）：系统状态 + 用户菜单。 */
export function ChatHeaderRight() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
      <SystemStatus />
      <UserMenu />
    </div>
  );
}

/** 管理页 Header 右侧统一内容：系统状态 + 用户菜单。 */
export function AdminHeaderRight() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
      <SystemStatus />
      <UserMenu />
    </div>
  );
}
