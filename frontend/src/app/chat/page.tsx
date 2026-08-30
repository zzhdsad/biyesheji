'use client';

import { useState } from 'react';
import { Button, Empty, Input, Layout, Typography } from 'antd';
import { PlusOutlined, SendOutlined } from '@ant-design/icons';
import { AppSider } from '@/components/layout/AppSider';
import { MessageItem } from '@/components/chat/MessageItem';
import { useChatStore } from '@/stores/chatStore';

const { Header, Content, Footer } = Layout;

export default function ChatPage() {
  const { messages, sendMessage } = useChatStore();
  const [input, setInput] = useState('');

  const handleSend = async () => {
    const text = input.trim();
    if (!text) return;
    setInput('');
    await sendMessage(text);
  };

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
          <Button type="primary" icon={<PlusOutlined />}>
            新建会话
          </Button>
        </Header>

        <Content style={{ padding: 24, overflow: 'auto' }}>
          {messages.length === 0 ? (
            <Empty description="基于内部知识的问答，答案附引用来源" style={{ marginTop: 120 }} />
          ) : (
            <div style={{ maxWidth: 900, margin: '0 auto' }}>
              {messages.map((m) => (
                <MessageItem key={m.id} message={m} />
              ))}
            </div>
          )}
        </Content>

        <Footer
          style={{
            padding: '12px 24px',
            background: '#fff',
            borderTop: '1px solid #f0f0f0',
          }}
        >
          <div style={{ maxWidth: 900, margin: '0 auto', display: 'flex', gap: 12 }}>
            <Input.TextArea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="输入问题，Enter 发送，Shift+Enter 换行"
              autoSize={{ minRows: 1, maxRows: 4 }}
              onPressEnter={(e) => {
                if (!e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
              }}
            />
            <Button type="primary" icon={<SendOutlined />} onClick={handleSend}>
              发送
            </Button>
          </div>
        </Footer>
      </Layout>
    </Layout>
  );
}
