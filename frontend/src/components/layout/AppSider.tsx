'use client';

import { App, Button, Divider, Dropdown, Layout, List, Spin, Typography, theme } from 'antd';
import {
  BulbFilled,
  BulbOutlined,
  CheckCircleFilled,
  DatabaseOutlined,
  DesktopOutlined,
  DownOutlined,
  HistoryOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import type { MenuProps } from 'antd';
import { useChatStore } from '@/stores/chatStore';
import { useThemeStore, type ThemeMode } from '@/stores/themeStore';

const { Sider } = Layout;

/** 主题下拉的选项文案。 */
const THEME_LABELS: Record<ThemeMode, string> = {
  light: '亮色模式',
  dark: '暗色模式',
  system: '跟随系统',
};

/** 左侧边栏：知识库列表 + 历史会话 + 主题切换（PRD 5.1 / 5.3）。 */
export function AppSider() {
  const {
    knowledgeBases,
    selectedKbId,
    selectKb,
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

  return (
    <Sider
      width={260}
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
      <Button
        type="dashed"
        icon={<PlusOutlined />}
        block
        onClick={() => newConversation()}
      >
        新建会话
      </Button>

      <Divider orientation="left" plain>
        <DatabaseOutlined /> 知识库
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
                <Typography.Text ellipsis style={{ maxWidth: 130, fontWeight: active ? 600 : 400 }}>
                  {kb.name}
                </Typography.Text>
                {active && <CheckCircleFilled style={{ color: token.colorPrimary, marginLeft: 4 }} />}
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
            return (
              <List.Item
                style={{
                  cursor: 'pointer',
                  background: active ? token.colorPrimaryBg : 'transparent',
                  borderRadius: token.borderRadiusSM,
                  paddingInline: 8,
                }}
                onClick={() => selectConversation(conv.id)}
              >
                <Typography.Text ellipsis style={{ maxWidth: 180, fontWeight: active ? 600 : 400 }}>
                  {conv.title}
                </Typography.Text>
              </List.Item>
            );
          }}
        />
      </Spin>

      {/* 弹性占位：把主题切换器压到底部 */}
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
          menu={{ items: themeMenuItems, onClick: onThemeMenuClick, selectable: true, selectedKeys: [themeMode] }}
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
