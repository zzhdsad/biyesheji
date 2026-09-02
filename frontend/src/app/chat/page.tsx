'use client';

import { useEffect, useRef, useState } from 'react';
import {
  Avatar,
  Badge,
  Button,
  Dropdown,
  Empty,
  Input,
  Layout,
  Select,
  Space,
  Tooltip,
  Typography,
} from 'antd';
import { LogoutOutlined, PlusOutlined, SendOutlined, UserOutlined } from '@ant-design/icons';
import type { MenuProps } from 'antd';
import { AppSider } from '@/components/layout/AppSider';
import { MessageItem } from '@/components/chat/MessageItem';
import { useChatStore } from '@/stores/chatStore';
import { useUserStore } from '@/stores/userStore';

const { Header, Content, Footer } = Layout;

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
      <Space style={{ cursor: 'pointer' }}>
        <Avatar size="small" icon={<UserOutlined />} />
        <Typography.Text>{user.username}</Typography.Text>
      </Space>
    </Dropdown>
  );
}

export default function ChatPage() {
  const {
    messages,
    sendMessage,
    sending,
    selectedKbId,
    knowledgeBases,
    selectKb,
    loadKnowledgeBases,
    loadConversations,
    loadHealth,
    newConversation,
  } = useChatStore();
  const [input, setInput] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);

  // 初始化：加载知识库、会话列表、系统状态；系统状态每 30s 轮询
  useEffect(() => {
    void loadKnowledgeBases();
    void loadConversations();
    void loadHealth();
    const t = setInterval(() => void loadHealth(), 30_000);
    return () => clearInterval(t);
  }, [loadKnowledgeBases, loadConversations, loadHealth]);

  // 新消息时自动滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length, sending]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !selectedKbId || sending) return;
    setInput('');
    await sendMessage(text);
  };

  const canSend = !!selectedKbId && !sending;

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <AppSider />
      <Layout>
        <Header
          style={{
            background: '#fff',
            borderBottom: '1px solid #f0f0f0',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            paddingInline: 24,
            height: 56,
            lineHeight: '56px',
          }}
        >
          <Typography.Title level={4} style={{ margin: 0, lineHeight: '56px' }}>
            智能问答
          </Typography.Title>
          <Space size="large" align="center">
            <Space size={8}>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                知识库
              </Typography.Text>
              <Select
                size="small"
                style={{ width: 180 }}
                placeholder="选择知识库"
                value={selectedKbId ?? undefined}
                onChange={selectKb}
                options={knowledgeBases.map((kb) => ({ label: kb.name, value: kb.id }))}
                notFoundContent="暂无知识库"
              />
            </Space>
            <SystemStatus />
            <Button icon={<PlusOutlined />} onClick={() => newConversation()}>
              新建会话
            </Button>
            <UserMenu />
          </Space>
        </Header>

        <Content style={{ padding: 24, overflow: 'auto' }}>
          {messages.length === 0 ? (
            <Empty
              description={
                selectedKbId
                  ? '基于所选知识库回答，答案附引用来源'
                  : '请在右上角选择知识库后再提问'
              }
              style={{ marginTop: 120 }}
            />
          ) : (
            <div style={{ maxWidth: 900, margin: '0 auto' }}>
              {messages.map((m) => (
                <MessageItem key={m.id} message={m} />
              ))}
              <div ref={bottomRef} />
            </div>
          )}
        </Content>

        <Footer
          style={{ padding: '12px 24px', background: '#fff', borderTop: '1px solid #f0f0f0' }}
        >
          <div style={{ maxWidth: 900, margin: '0 auto', display: 'flex', gap: 12 }}>
            <Input.TextArea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                selectedKbId
                  ? '输入问题，Enter 发送，Shift+Enter 换行'
                  : '请先在右上角选择知识库'
              }
              autoSize={{ minRows: 1, maxRows: 4 }}
              disabled={!selectedKbId}
              onPressEnter={(e) => {
                if (!e.shiftKey) {
                  e.preventDefault();
                  void handleSend();
                }
              }}
            />
            <Tooltip title={!selectedKbId ? '请先选择知识库' : ''}>
              <Button
                type="primary"
                icon={<SendOutlined />}
                onClick={() => void handleSend()}
                disabled={!canSend}
                loading={sending}
              >
                发送
              </Button>
            </Tooltip>
          </div>
        </Footer>
      </Layout>
    </Layout>
  );
}
