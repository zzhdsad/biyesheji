'use client';

import { useState, type ReactNode } from 'react';
import { Avatar, Collapse, Tag, Typography } from 'antd';
import { RobotOutlined, UserOutlined, FileTextOutlined } from '@ant-design/icons';
import type { Citation, ChatMessage } from '@/types';

const { Text, Paragraph } = Typography;

const CITATION_RE = /\[citation:\s*(\d+)(?:\s*,\s*(\d+))?\s*\]/gi;

/** 将答案中的 [citation: n, p] 标记渲染为可点击的内联引用 chip。 */
function renderAnswer(
  content: string,
  citations: Citation[],
  onChip: (n: number) => void,
  active: number | null,
): ReactNode[] {
  const nodes: ReactNode[] = [];
  const byIndex = new Map(citations.map((c) => [c.source_index, c]));
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  CITATION_RE.lastIndex = 0;
  while ((m = CITATION_RE.exec(content)) !== null) {
    if (m.index > last) {
      nodes.push(<span key={key++}>{content.slice(last, m.index)}</span>);
    }
    const num = parseInt(m[1], 10);
    const exists = byIndex.has(num);
    nodes.push(
      <Tag
        key={key++}
        onClick={() => exists && onChip(num)}
        style={{
          margin: '0 2px',
          cursor: exists ? 'pointer' : 'default',
          userSelect: 'none',
          background: active === num ? '#1677ff' : exists ? '#e6f4ff' : '#f5f5f5',
          color: active === num ? '#fff' : exists ? '#1677ff' : '#bfbfbf',
          border: 'none',
          borderRadius: 4,
          fontSize: 12,
          padding: '0 6px',
        }}
      >
        [{num}]
      </Tag>,
    );
    last = CITATION_RE.lastIndex;
  }
  if (last < content.length) {
    nodes.push(<span key={key++}>{content.slice(last)}</span>);
  }
  return nodes;
}

/** 引用卡片头部：文档名 · 页码 · 标题路径 + 相似度。 */
function cardLabel(c: Citation): ReactNode {
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
      <Tag color="blue" style={{ margin: 0, fontWeight: 500 }}>
        [{c.source_index}]
      </Tag>
      <FileTextOutlined style={{ color: '#1677ff' }} />
      <Text strong style={{ marginRight: 4 }}>
        {c.doc_name}
      </Text>
      {c.page_num != null && c.page_num > 0 && (
        <Text type="secondary">第 {c.page_num} 页</Text>
      )}
      {c.title_path && <Text type="secondary" style={{ fontSize: 12 }}>· {c.title_path}</Text>}
      {c.score != null && (
        <Tag style={{ margin: 0, fontSize: 12, color: '#8c8c8c', background: '#fafafa', border: 'none' }}>
          相关度 {(c.score * 100).toFixed(0)}%
        </Tag>
      )}
    </div>
  );
}

/** 单条消息气泡 + 引用来源卡片。 */
export function MessageItem({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';
  const [activeKey, setActiveKey] = useState<string[]>([]);

  const citations = message.citations ?? [];

  return (
    <div
      style={{
        display: 'flex',
        justifyContent: isUser ? 'flex-end' : 'flex-start',
        marginBottom: 16,
        gap: 12,
      }}
    >
      {!isUser && <Avatar icon={<RobotOutlined />} style={{ background: '#1677ff' }} />}
      <div style={{ maxWidth: '70%' }}>
        <div
          style={{
            background: isUser ? '#1677ff' : '#f5f5f5',
            color: isUser ? '#fff' : 'rgba(0,0,0,0.88)',
            padding: '10px 16px',
            borderRadius: 12,
            lineHeight: 1.8,
          }}
        >
          {isUser ? (
            <span style={{ whiteSpace: 'pre-wrap' }}>{message.content}</span>
          ) : message.content ? (
            <div style={{ whiteSpace: 'pre-wrap' }}>
              {renderAnswer(
                message.content,
                citations,
                (n) => setActiveKey([String(n)]),
                activeKey[0] ? parseInt(activeKey[0], 10) : null,
              )}
            </div>
          ) : (
            // 流式生成前的检索阶段：占位提示，首个 delta 到达后由打字机内容替代
            <span style={{ color: '#8c8c8c' }}>正在检索知识库…</span>
          )}
        </div>
        {!isUser && citations.length > 0 && (
          <div style={{ marginTop: 8 }}>
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>
              引用来源（点击展开原文）
            </Text>
            <Collapse
              activeKey={activeKey}
              onChange={setActiveKey}
              size="small"
              style={{ marginTop: 4, background: '#fafafa', borderRadius: 8 }}
              items={citations.map((c) => ({
                key: String(c.source_index),
                label: cardLabel(c),
                children: (
                  <Paragraph
                    style={{ margin: 0, whiteSpace: 'pre-wrap', color: 'rgba(0,0,0,0.75)', fontSize: 13 }}
                    ellipsis={{ rows: 6, expandable: true, symbol: '展开全文' }}
                  >
                    {c.content}
                  </Paragraph>
                ),
              }))}
            />
          </div>
        )}
      </div>
      {isUser && <Avatar icon={<UserOutlined />} style={{ background: '#8c8c8c' }} />}
    </div>
  );
}
