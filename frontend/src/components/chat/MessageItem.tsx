'use client';

import { useState, type ReactNode } from 'react';
import { Avatar, Typography } from 'antd';
import { RobotOutlined, UserOutlined } from '@ant-design/icons';
import type { ChatMessage } from '@/types';
import { resolveCitationKey } from '@/utils/evidence';
import { EvidencePanel } from './EvidencePanel';
import { FeedbackButtons } from './FeedbackButtons';

const { Paragraph, Text } = Typography;

/** 将内联引用标记 [citation: 编号, 页码] 渲染为可点击 chip，点击后展开对应原文。 */
function renderAnswer(content: string, onChip: (index: number) => void): ReactNode[] {
  const parts: ReactNode[] = [];
  let rest = content;
  let i = 0;
  while (rest.length > 0) {
    const m = rest.match(/\[citation:\s*(\d+)(?:,\s*(-?\d+))?\]/);
    if (!m || m.index == null) break;
    if (m.index > 0) parts.push(<span key={`t${i}`}>{rest.slice(0, m.index)}</span>);
    const index = Number(m[1]);
    const page = m[2] != null ? m[2].trim() : '';
    parts.push(
      <sup
        key={`c${i}`}
        role="button"
        tabIndex={0}
        onClick={() => onChip(index)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') onChip(index);
        }}
        style={{
          margin: '0 2px',
          padding: '0 6px',
          borderRadius: 10,
          background: '#e6f4ff',
          color: '#1677ff',
          border: '1px solid #91caff',
          fontSize: 12,
          cursor: 'pointer',
        }}
        title="查看引用来源"
      >
        {page ? `${page}` : `[${index}]`}
      </sup>,
    );
    rest = rest.slice(m.index + m[0].length);
    i += 1;
  }
  if (rest.length > 0) parts.push(<span key="tail">{rest}</span>);
  return parts;
}

export function MessageItem({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';
  const [activeKey, setActiveKey] = useState<string[]>([]);

  // BUG-058：内联引用编号（LLM 生成）→ 证据面板 key（后端 source_index）映射。
  // 越界编号没有对应来源 → 忽略点击（旧实现会点不动且无法收起）。
  const sourceIndexes = (message.citations ?? []).map((c) => c.source_index);
  const onChip = (index: number) => {
    const key = resolveCitationKey(index, sourceIndexes);
    if (!key) return;
    // 同一个 chip 再点一次收起（旧实现永远 set 成同一个 key，无法收起）
    setActiveKey((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  };

  return (
    <div style={{ display: 'flex', gap: 12, justifyContent: isUser ? 'flex-end' : 'flex-start' }}>
      {!isUser && (
        <Avatar
          size={32}
          style={{ backgroundColor: '#1677ff', flexShrink: 0 }}
          icon={<RobotOutlined />}
        />
      )}
      <div style={{ maxWidth: '78%' }}>
        <div
          style={{
            padding: '10px 14px',
            borderRadius: 12,
            background: isUser ? '#1677ff' : '#fff',
            color: isUser ? '#fff' : 'rgba(0,0,0,0.88)',
            boxShadow: '0 1px 2px rgba(0,0,0,0.06)',
            border: isUser ? 'none' : '1px solid #f0f0f0',
          }}
        >
          {isUser ? (
            <Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', color: '#fff' }}>
              {message.content}
            </Paragraph>
          ) : (
            <Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
              {/* BUG-053：历史脏数据 content=null 会让 renderAnswer 直接抛错（白屏） */}
              {renderAnswer(message.content ?? '', onChip)}
            </Paragraph>
          )}
        </div>
        {/* BUG-057：后端 /feedbacks 已实现，但组件此前全项目无引用 → 接入助手消息 */}
        {!isUser && <FeedbackButtons messageId={message.id} />}
        {!isUser &&
          message.queryAnalysis &&
          // 阶段十一：仅开发环境展示 Query Analysis 调试信息（不改动 Chat UI 主体）
          process.env.NODE_ENV !== 'production' && (
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
              Query: {message.queryAnalysis.question_type_label}
              {message.queryAnalysis.resource_types?.length
                ? ` · ${message.queryAnalysis.resource_types.join('/')}`
                : ''}
              {message.queryAnalysis.is_multi_source ? ' · 多来源' : ''}
              {message.queryAnalysis.is_unanswerable_candidate ? ' · 可能无依据' : ''}
              {message.queryAnalysis.is_valid === false ? ' · 分析兜底' : ''}
            </Text>
          )}
        {!isUser &&
          message.routerDecision &&
          // 阶段十二：仅开发环境展示检索策略调试信息（不改动 Chat UI 主体）
          process.env.NODE_ENV !== 'production' && (
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
              Strategy: {message.routerDecision.strategy_name}
              {message.routerDecision.is_valid === false ? ' · 路由兜底' : ''}
            </Text>
          )}
        {!isUser && message.citations && message.citations.length > 0 && (
          // 阶段十：多来源证据分组展示（文档 / 中药 / 方剂 / 理论 / 文献）
          <EvidencePanel
            citations={message.citations}
            groups={message.evidenceGroups}
            activeKey={activeKey}
            onChange={(keys) => setActiveKey(keys)}
          />
        )}
      </div>
      {isUser && <Avatar size={32} style={{ flexShrink: 0 }} icon={<UserOutlined />} />}
    </div>
  );
}
