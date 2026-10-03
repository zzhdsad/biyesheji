'use client';

import { useEffect, useRef, useState } from 'react';
import { App, Input, Space, Tag, Tooltip, Typography, Button, Select } from 'antd';
import { SendOutlined } from '@ant-design/icons';
import { AppLayout } from '@/components/layout/AppLayout';
import { ChatHeaderRight } from '@/components/layout/AppSider';
import { MessageItem } from '@/components/chat/MessageItem';
import { ChatWelcome } from '@/components/chat/ChatWelcome';
import { useChatStore } from '@/stores/chatStore';

/** 与后端 chat.py `ChatAskRequest.question` 的 max_length=2000 保持一致（BUG-052）。 */
const QUESTION_MAX_LENGTH = 2000;

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
  const { message } = App.useApp();

  const hasKb = knowledgeBases.length > 0;

  // 初始化：加载知识库、会话列表（系统健康状态由 AppSider 全局加载）
  useEffect(() => {
    void loadKnowledgeBases();
    void loadConversations();
  }, [loadKnowledgeBases, loadConversations]);

  // 模块 C：承接首页「即问」入口——读取 ?q= 预填输入框（不自动发送，
  // 避免误触发长耗时检索），随后清除 URL 参数避免刷新时重复带入。
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const q = params.get('q');
    if (q) {
      setInput(q.slice(0, QUESTION_MAX_LENGTH));
      window.history.replaceState(null, '', '/chat');
    }
  }, []);

  // 新消息时自动滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length, sending]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !hasKb || sending) return;
    // BUG-052：超出后端上限时前端直接拦截（旧实现要等提交后才返回 422）
    if (text.length > QUESTION_MAX_LENGTH) {
      message.warning(`问题最长 ${QUESTION_MAX_LENGTH} 字，当前 ${text.length} 字`);
      return;
    }
    setInput('');
    await sendMessage(text);
  };

  const canSend = hasKb && !sending;

  // 问题3修复：员工角色若无可见知识库时给出明确提示，而不是静默不可用
  const kbEmptyHint = !hasKb
    ? '暂无可用的知识库，请联系管理员将你加入知识库或创建公开知识库'
    : '';

  // 欢迎界面与聊天界面互斥（问题三）：没有消息 → AI 助手欢迎页（自带
  // 中央输入框，不渲染底部 footer）；有消息 → 正常聊天布局（footer 输入）。
  const isEmpty = messages.length === 0;

  // 产品化 Header（问题五）：左 = 标题；右 = 知识库选择器 + 用户菜单
  // （系统状态已弱化到用户菜单内部，不再作为主要视觉元素）。
  const headerLeft = (
    <Typography.Title level={4} style={{ margin: 0 }}>
      智能问答
    </Typography.Title>
  );

  const headerRight = (
    <Space size={12} align="center">
      <Select
        mode="multiple"
        maxTagCount="responsive"
        size="small"
        style={{ minWidth: 200, maxWidth: 360 }}
        placeholder="知识范围：全部知识库"
        value={selectedKbIds}
        onChange={setSelectedKbIds}
        options={knowledgeBases.map((kb) => ({ label: kb.name, value: kb.id }))}
        notFoundContent="暂无知识库"
        suffixIcon={
          selectedKbIds.length === 0 ? (
            <Tag color="blue" style={{ margin: 0, fontSize: 12 }}>
              全部
            </Tag>
          ) : null
        }
      />
      {selectedKbIds.length > 0 && (
        <Button size="small" type="link" onClick={resetKbSelection}>
          重置
        </Button>
      )}
      <ChatHeaderRight />
    </Space>
  );

  const content = isEmpty ? (
    <ChatWelcome
      value={input}
      onChange={setInput}
      onSend={() => void handleSend()}
      disabled={!hasKb}
      disabledHint={kbEmptyHint}
      scopeLabel={
        selectedKbIds.length === 0
          ? '全部知识库'
          : `已选 ${selectedKbIds.length} 个知识库`
      }
      onPickExample={(q) => setInput(q)}
    />
  ) : (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      {messages.map((m) => (
        <MessageItem key={m.id} message={m} />
      ))}
      <div ref={bottomRef} />
    </div>
  );

  // footer 输入区仅在聊天模式渲染；欢迎界面自带中央输入框，二者不同时存在
  const footer = isEmpty
    ? undefined
    : (() => (
        <div style={{ maxWidth: 900, margin: '0 auto', display: 'flex', gap: 12 }}>
          <Input.TextArea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="输入问题，Enter 发送，Shift+Enter 换行"
            autoSize={{ minRows: 1, maxRows: 4 }}
            disabled={!hasKb}
            // BUG-052：与后端 max_length 对齐，输入即限长（并显示计数）
            maxLength={QUESTION_MAX_LENGTH}
            showCount
            onPressEnter={(e) => {
              if (!e.shiftKey) {
                e.preventDefault();
                void handleSend();
              }
            }}
          />
          <Tooltip title={!hasKb ? kbEmptyHint || '请先创建知识库' : ''}>
            <Button
              type="primary"
              icon={<SendOutlined />}
              onClick={() => void handleSend()}
              disabled={!canSend}
              loading={sending}
              style={
                !canSend
                  ? { opacity: 0.45, cursor: 'not-allowed', boxShadow: 'none' }
                  : undefined
              }
            >
              发送
            </Button>
          </Tooltip>
        </div>
      ))();

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

