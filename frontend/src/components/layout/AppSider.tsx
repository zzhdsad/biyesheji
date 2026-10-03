'use client';

import { useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import {
  App,
  Avatar,
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
  BookOutlined,
  BulbFilled,
  BulbOutlined,
  ControlOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  AuditOutlined,
  DesktopOutlined,
  DeleteOutlined,
  DownOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  HistoryOutlined,
  HomeOutlined,
  ImportOutlined,
  LogoutOutlined,
  MedicineBoxOutlined,
  MessageOutlined,
  PlusOutlined,
  ProfileOutlined,
  ReadOutlined,
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

/**
 * 顶部主导航：按用户任务（而非数据库表）组织信息架构。
 *
 * - 智能问答：系统核心入口，所有用户可见；
 * - 知识资源：中药/方剂/理论/文献的浏览入口，所有用户可见
 *   （页面内已有按钮级权限：member 只读浏览，admin 可编辑）；
 * - 管理中心：后台运维功能，仅 admin 可见（member 不应看到管理入口；
 *   页面级权限仍由后端 403 兜底，此处只做导航收起）。
 *
 * key 即路由路径。
 */
const CORE_NAV_ITEMS: Required<MenuProps>['items'] = [
  { key: '/', icon: <HomeOutlined />, label: '首页' },
  // 智能问答沿用整页跳转（沿用旧行为：切换会话后 zustand 状态需整页重载，避免残留）
  // key 必须是 '/chat'（带斜杠）才能参与 matchNavKey 的路由高亮
  {
    key: '/chat',
    icon: <MessageOutlined />,
    label: '智能问答',
    onClick: () => window.location.assign('/chat'),
  },
];

const RESOURCE_NAV_ITEMS: Required<MenuProps>['items'] = [
  {
    key: 'resource-group',
    label: (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        <BookOutlined /> 知识资源
      </span>
    ),
    type: 'group',
    children: [
      { key: '/herbs', icon: <MedicineBoxOutlined />, label: '中药' },
      { key: '/prescriptions', icon: <ProfileOutlined />, label: '方剂' },
      { key: '/theories', icon: <ReadOutlined />, label: '中医理论' },
      { key: '/literatures', icon: <BookOutlined />, label: '文献' },
    ],
  },
];

const ADMIN_NAV_ITEMS: Required<MenuProps>['items'] = [
  {
    key: 'admin-center',
    icon: <SettingOutlined />,
    label: '管理中心',
    children: [
      { key: '/kb', icon: <DatabaseOutlined />, label: '知识库管理' },
      {
        key: 'data-mgmt',
        label: '数据管理',
        children: [
          { key: '/documents', icon: <FileTextOutlined />, label: '文档管理' },
          { key: '/taxonomy', icon: <AppstoreOutlined />, label: '分类与标签' },
          { key: '/admin/import', icon: <ImportOutlined />, label: '数据导入中心' },
          { key: '/evaluation', icon: <ExperimentOutlined />, label: '评估面板' },
        ],
      },
      { key: '/users', icon: <TeamOutlined />, label: '用户管理' },
      {
        key: 'system-mgmt',
        label: '系统管理',
        children: [
          { key: '/admin', icon: <DashboardOutlined />, label: '系统仪表盘' },
          { key: '/audit', icon: <AuditOutlined />, label: '审计日志' },
          { key: '/settings', icon: <ControlOutlined />, label: '系统设置' },
        ],
      },
    ],
  },
];

/**
 * 侧边栏可被选中的路由 key（自动从菜单展开，**不含分组/父级节点**）。
 * 新增菜单项时无需再改任何匹配逻辑——匹配完全由数据结构递归推导。
 */
export const NAV_ROUTE_KEYS: string[] = (
  [...(CORE_NAV_ITEMS ?? []), ...(RESOURCE_NAV_ITEMS ?? []), ...(ADMIN_NAV_ITEMS ?? [])] as Required<MenuProps>['items']
).flatMap(function flatten(item): Required<MenuProps>['items'] {
  if (!item || !('children' in item) || !Array.isArray(item.children)) return [item];
  // 分组节点（type: 'group'）自身不是路由，子项递归展平；submenu 父级同理
  return (item.children as Required<MenuProps>['items']).flatMap(flatten);
}).map((item) => String((item as { key?: string })?.key ?? '')).filter((key) => key.startsWith('/'));

/**
 * 统一路由匹配：**最长前缀命中**。
 *
 * BUG 修复：此前是一串 `pathname.startsWith('/xxx')` 的 if-else，`/admin`
 * 排在 `/admin/import` 之前，导致进入「数据导入中心」时高亮的是「系统仪表盘」；
 * 同时 `/taxonomy` 根本没有分支，分类与标签页永远不高亮。
 * 这里改为按菜单 key 做最长前缀匹配，子路由（/admin/import/xxx）也会保持
 * 父级菜单高亮，且不写死任何页面名称。
 *
 * BUG 修复（高亮"首页"）：根键 '/' 不得做前缀匹配——`'/chat'.startsWith('/')`
 * 恒为真会让任何页面都命中「首页」。根路由只在完全相等时命中。
 */
export function matchNavKey(pathname: string | null | undefined): string {
  const path = (pathname || '/').replace(/\/+$/, '') || '/';
  let best = '';
  for (const key of NAV_ROUTE_KEYS) {
    const candidate = key.replace(/\/+$/, '') || '/';
    if (candidate === '/') {
      // 根键仅精确匹配：'/' 命中首页，'/chat' 等不再误中
      if (path === '/') best = '/';
      continue;
    }
    if (path === candidate || path.startsWith(`${candidate}/`)) {
      if (candidate.length > best.length) best = candidate;
    }
  }
  return best || '/';
}

/** 右上角用户菜单：头像 + 用户名，下拉（含弱化的系统状态项）+ 登出。 */
function UserMenu() {
  const { user, logout } = useUserStore();
  const { health } = useChatStore();
  if (!user) return null;
  const comps = health?.components ?? {};
  const allOk =
    Object.keys(comps).length > 0 && Object.values(comps).every((v) => v === 'ok');
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
      // 产品化 Header：系统状态不再作为主要视觉元素，
      // 弱化为用户菜单里的一行只读提示
      key: 'health',
      disabled: true,
      label: (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          系统状态：{allOk ? '正常' : health ? '部分组件异常' : '未知'}
        </Typography.Text>
      ),
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
  // 菜单信息架构：管理中心仅 admin 可见（member 只看到问答与知识资源）
  const user = useUserStore((s) => s.user);
  const isAdmin = user?.role === 'admin';

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

  // 会话区（新建会话 + 历史会话）只在智能问答页显示：
  // 会话是问答功能的内部能力，不再作为与"智能问答"并列的全站一级入口
  const isChatPage = pathname === '/chat';

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

  // 当前在哪个管理页？（统一最长前缀匹配，见 matchNavKey 注释）
  const selectedNavKeys: string[] = [matchNavKey(pathname)];

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
        onClick={() => router.push('/')}
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

      {/* 主导航：首页/智能问答 + 知识资源 + 管理中心（仅 admin） */}
      <Menu
        mode="inline"
        selectedKeys={selectedNavKeys}
        items={[
          ...(CORE_NAV_ITEMS ?? []),
          ...(RESOURCE_NAV_ITEMS ?? []),
          ...(isAdmin ? (ADMIN_NAV_ITEMS ?? []) : []),
        ]}
        onClick={onNavClick}
        style={{
          background: 'transparent',
          borderInlineEnd: 'none',
          marginBottom: 8,
        }}
      />

      {/* 会话区：仅在智能问答页显示（会话是问答功能的内部能力） */}
      {isChatPage && (
        <>
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
                  void selectConversation(conv.id);
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
        </>
      )}

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

/** 统一右侧栏 Header 内容（全站通用）：用户菜单（系统状态已弱化进菜单内部）。 */
export function ChatHeaderRight() {
  return <UserMenu />;
}

/** 管理页 Header 右侧统一内容：用户菜单。 */
export function AdminHeaderRight() {
  return <UserMenu />;
}
