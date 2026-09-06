'use client';

import { useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import {
  App,
  Avatar,
  Badge,
  Button,
  Divider,
  Dropdown,
  Form,
  Input,
  Layout,
  List,
  Menu,
  Modal,
  Radio,
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
  CheckCircleFilled,
  ControlOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  DesktopOutlined,
  DownOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  HistoryOutlined,
  LogoutOutlined,
  MessageOutlined,
  PlusOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useChatStore } from '@/stores/chatStore';
import { useThemeStore, type ThemeMode } from '@/stores/themeStore';
import { useUserStore } from '@/stores/userStore';

interface KbFormValues {
  name: string;
  description?: string;
  visibility: 'public' | 'private';
}

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
 * 3. 知识库列表（+ 新建入口 + 选中状态 + hover 管理文档按钮）
 * 4. 历史会话列表
 * 5. 底部：刷新会话 / 主题切换
 */
export function AppSider() {
  const {
    knowledgeBases,
    selectedKbId,
    selectKb,
    createKb,
    conversations,
    currentConversationId,
    selectConversation,
    newConversation,
    loadingKbs,
    loadingConversations,
    loadConversations,
  } = useChatStore();

  const themeMode = useThemeStore((s) => s.mode);
  const setThemeMode = useThemeStore((s) => s.setMode);
  const { message } = App.useApp();
  const router = useRouter();
  const pathname = usePathname();

  // token 化配色：跟随 ConfigProvider 算法自动亮/暗切换（不再硬编码浅色值）
  const { token } = theme.useToken();

  // 新建知识库弹窗状态
  const [kbModalOpen, setKbModalOpen] = useState(false);
  const [kbSubmitting, setKbSubmitting] = useState(false);
  const [kbForm] = Form.useForm<KbFormValues>();

  const onKbCreate = async () => {
    try {
      const values = await kbForm.validateFields();
      setKbSubmitting(true);
      const kb = await createKb(values.name, values.description, values.visibility);
      if (kb) {
        message.success(`知识库「${kb.name}」创建成功`);
        setKbModalOpen(false);
        kbForm.resetFields();
      } else {
        message.error('创建失败，请稍后再试');
      }
    } catch {
      // 表单校验失败，antd 自动提示
    } finally {
      setKbSubmitting(false);
    }
  };

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
        <DatabaseOutlined /> 知识库
        <Button
          type="text"
          size="small"
          icon={<PlusOutlined />}
          aria-label="新建知识库"
          onClick={() => setKbModalOpen(true)}
          style={{ padding: '0 4px', marginLeft: 4 }}
        />
      </Divider>
      <Spin spinning={loadingKbs} size="small">
        <List
          size="small"
          dataSource={knowledgeBases}
          locale={{ emptyText: '暂无知识库' }}
          renderItem={(kb) => {
            const active = kb.id === selectedKbId;
            return (
              <List.Item
                style={{
                  cursor: 'pointer',
                  background: active ? token.colorPrimaryBg : 'transparent',
                  borderRadius: token.borderRadiusSM,
                  paddingInline: 8,
                }}
                onClick={() => selectKb(kb.id)}
              >
                <div
                  className="group"
                  style={{ display: 'flex', alignItems: 'center', width: '100%', gap: 4 }}
                >
                  <Typography.Text
                    ellipsis
                    style={{ flex: 1, minWidth: 0, fontWeight: active ? 600 : 400 }}
                  >
                    {kb.name}
                  </Typography.Text>
                  {active && <CheckCircleFilled style={{ color: token.colorPrimary }} />}
                  <Tooltip title="管理文档">
                    <Button
                      type="text"
                      size="small"
                      icon={<FileTextOutlined />}
                      className="opacity-0 group-hover:opacity-100"
                      style={{ padding: '0 4px', flex: 'none' }}
                      onClick={(e) => {
                        e.stopPropagation();
                        selectKb(kb.id);
                        router.push(`/documents?kb_id=${kb.id}`);
                      }}
                    />
                  </Tooltip>
                </div>
              </List.Item>
            );
          }}
        />
      </Spin>

      <Divider orientation="left" plain>
        <HistoryOutlined /> 历史会话
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
                style={{
                  cursor: 'pointer',
                  background: active ? token.colorPrimaryBg : 'transparent',
                  borderRadius: token.borderRadiusSM,
                  paddingInline: 8,
                }}
                onClick={onClick}
              >
                <Typography.Text
                  ellipsis
                  style={{ maxWidth: 220, fontWeight: active ? 600 : 400 }}
                >
                  {conv.title}
                </Typography.Text>
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

      {/* 新建知识库弹窗（POST /kb，owner_id 后端取当前登录用户） */}
      <Modal
        title="新建知识库"
        open={kbModalOpen}
        onOk={onKbCreate}
        onCancel={() => {
          setKbModalOpen(false);
          kbForm.resetFields();
        }}
        okText="创建"
        cancelText="取消"
        confirmLoading={kbSubmitting}
        destroyOnClose
      >
        <Form<KbFormValues>
          form={kbForm}
          layout="vertical"
          initialValues={{ visibility: 'private' }}
          preserve={false}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入知识库名称' },
              { max: 128, message: '名称最长 128 字' },
            ]}
          >
            <Input placeholder="如：员工手册" autoFocus />
          </Form.Item>
          <Form.Item name="description" label="描述（可选）">
            <Input.TextArea
              placeholder="简述该知识库的用途与范围"
              autoSize={{ minRows: 2, maxRows: 4 }}
              maxLength={2000}
              showCount
            />
          </Form.Item>
          <Form.Item name="visibility" label="可见性">
            <Radio.Group>
              <Radio value="private">私有（仅自己可见）</Radio>
              <Radio value="public">公开（所有人可读）</Radio>
            </Radio.Group>
          </Form.Item>
        </Form>
      </Modal>
    </Sider>
  );
}

/** 统一右侧栏 Header 内容（聊天页用）：左侧标题、右侧系统状态/新建会话/用户菜单。 */
export function ChatHeaderRight({ onNew }: { onNew: () => void }) {
  return (
    <>
      <SystemStatus />
      <Button icon={<PlusOutlined />} onClick={onNew}>
        新建会话
      </Button>
      <UserMenu />
    </>
  );
}

/** 管理页 Header 右侧统一内容：系统状态 + 用户菜单（聊天页多了"新建会话"按钮，这里只保留 2 项）。 */
export function AdminHeaderRight() {
  return (
    <>
      <SystemStatus />
      <UserMenu />
    </>
  );
}
