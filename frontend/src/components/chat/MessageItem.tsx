'use client';

import { Avatar, Tag, Typography } from 'antd';
import { RobotOutlined, UserOutlined } from '@ant-design/icons';
import type { ChatMessage } from '@/types';

/** 单条消息气泡 + 引用来源卡片。 */
export function MessageItem({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';

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
            whiteSpace: 'pre-wrap',
          }}
        >
          {message.content}
        </div>
        {!isUser && message.citations && message.citations.length > 0 && (
          <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {message.citations.map((c) => (
              <Tag key={c.doc_id} color="blue">
                {c.doc_name}
                {c.page_num != null ? ` · 第${c.page_num}页` : ''}
              </Tag>
            ))}
          </div>
        )}
      </div>
      {isUser && <Avatar icon={<UserOutlined />} style={{ background: '#8c8c8c' }} />}
    </div>
  );
}
