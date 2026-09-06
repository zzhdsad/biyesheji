'use client';

import { useEffect, useRef, useState } from 'react';
import { Empty, Input, Space, Tooltip, Typography, Button, Select } from 'antd';
import { SendOutlined, PlusOutlined } from '@ant-design/icons';
import { AppLayout } from '@/components/layout/AppLayout';
import { ChatHeaderRight } from '@/components/layout/AppSider';
import { MessageItem } from '@/components/chat/MessageItem';
import { useChatStore } from '@/stores/chatStore';

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

  const headerLeft = (
    <>
      <Typography.Title level={4} style={{ margin: 0 }}>
        智能问答
      </Typography.Title>
      <Space size={8}>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          知识库
        </Typography.Text>
        <Select
          size="small"
          style={{ width: 200 }}
          placeholder="选择知识库"
          value={selectedKbId ?? undefined}
          onChange={selectKb}
          options={knowledgeBases.map((kb) => ({ label: kb.name, value: kb.id }))}
          notFoundContent="暂无知识库"
        />
      </Space>
    </>
  );

  const headerRight = (
    <Space size="large" align="center">
      <ChatHeaderRight onNew={newConversation} />
    </Space>
  );

  const content = (
    <>
      {messages.length === 0 ? (
        <Empty
          description={
            selectedKbId
              ? '基于所选知识库回答，答案附引用来源'
              : '请在顶部选择知识库后再提问'
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
    </>
  );

  const footer = (
    <div style={{ maxWidth: 900, margin: '0 auto', display: 'flex', gap: 12 }}>
      <Input.TextArea
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder={
          selectedKbId
            ? '输入问题，Enter 发送，Shift+Enter 换行'
            : '请先在顶部选择知识库'
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
  );

  return (
    <AppLayout
      headerLeft={headerLeft}
      headerRight={headerRight}
      footer={footer}
      contentStyle={{ padding: 24, overflow: 'auto' }}
    >
      {content}
    </AppLayout>
  );
}
