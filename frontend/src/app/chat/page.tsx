'use client';

import { useEffect, useRef, useState } from 'react';
import { Empty, Input, Space, Tooltip, Typography, Button, Select, Tag } from 'antd';
import { SendOutlined } from '@ant-design/icons';
import { AppLayout } from '@/components/layout/AppLayout';
import { ChatHeaderRight } from '@/components/layout/AppSider';
import { MessageItem } from '@/components/chat/MessageItem';
import { useChatStore } from '@/stores/chatStore';

export default function ChatPage() {
  const {
    messages,
    sendMessage,
    sending,
    selectedKbIds,
    knowledgeBases,
    setSelectedKbIds,
    resetKbSelection,
    loadKnowledgeBases,
    loadConversations,
  } = useChatStore();
  const [input, setInput] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);

  const hasKb = knowledgeBases.length > 0;

  // 初始化：加载知识库、会话列表（系统健康状态由 AppSider 全局加载）
  useEffect(() => {
    void loadKnowledgeBases();
    void loadConversations();
  }, [loadKnowledgeBases, loadConversations]);

  // 新消息时自动滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length, sending]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !hasKb || sending) return;
    setInput('');
    await sendMessage(text);
  };

  const canSend = hasKb && !sending;

  const headerLeft = (
    <>
      <Typography.Title level={4} style={{ margin: 0 }}>
        智能问答
      </Typography.Title>
      <Space size={8} align="center">
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          知识库
        </Typography.Text>
        <Select
          mode="multiple"
          maxTagCount="responsive"
          size="small"
          style={{ minWidth: 220, maxWidth: 400 }}
          placeholder="默认全部知识库"
          value={selectedKbIds}
          onChange={setSelectedKbIds}
          options={knowledgeBases.map((kb) => ({ label: kb.name, value: kb.id }))}
          notFoundContent="暂无知识库"
          suffixIcon={
            selectedKbIds.length === 0 ? (
              <Tag color="blue" style={{ margin: 0, fontSize: 12 }}>全部</Tag>
            ) : null
          }
        />
        {selectedKbIds.length > 0 && (
          <Button size="small" type="link" onClick={resetKbSelection}>
            重置
          </Button>
        )}
      </Space>
    </>
  );

  const headerRight = <ChatHeaderRight />;

  const content = (
    <>
      {messages.length === 0 ? (
        <Empty
          description={
            hasKb
              ? '基于所选知识库回答，答案附引用来源'
              : '暂无可用知识库，请先创建或上传文档'
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
          hasKb
            ? '输入问题，Enter 发送，Shift+Enter 换行'
            : '请先创建知识库并上传文档'
        }
        autoSize={{ minRows: 1, maxRows: 4 }}
        disabled={!hasKb}
        onPressEnter={(e) => {
          if (!e.shiftKey) {
            e.preventDefault();
            void handleSend();
          }
        }}
      />
      <Tooltip title={!hasKb ? '请先创建知识库' : ''}>
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
