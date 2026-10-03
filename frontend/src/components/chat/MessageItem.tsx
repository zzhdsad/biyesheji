'use client';

import { useState, type ReactNode } from 'react';
import { Avatar, Typography, theme } from 'antd';
import {
  CheckCircleOutlined,
  LoadingOutlined,
  RobotOutlined,
  UserOutlined,
} from '@ant-design/icons';
import type { ChatMessage } from '@/types';
import { parseCitationMarkers, resolveCitationKey } from '@/utils/evidence';
import { EvidencePanel } from './EvidencePanel';
import { FeedbackButtons } from './FeedbackButtons';

const { Paragraph, Text } = Typography;

/** 轻量"三点思考"动画：纯 CSS 实现，不引入动画库。 */
function ThinkingDots({ color }: { color: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 3, marginLeft: 2 }}>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          style={{
            width: 4,
            height: 4,
            borderRadius: '50%',
            background: color,
            display: 'inline-block',
            animation: 'kp-thinking 1.2s ease-in-out infinite',
            animationDelay: `${i * 0.2}s`,
          }}
        />
      ))}
      {/* 关键帧全局注入一次即可 */}
      <style>{`
        @keyframes kp-thinking {
          0%, 60%, 100% { opacity: 0.25; transform: translateY(0); }
          30% { opacity: 1; transform: translateY(-2px); }
        }
      `}</style>
    </span>
  );
}

/**
 * 用户可见的「回答过程」步骤（问题8/9）：像 DeepSeek 一样展示 AI 正在做什么。
 *
 * 全部由**真实 SSE 事件**驱动（send / start / citations / 第一个 delta），
 * 不暴露模型隐藏思维链、不伪造任何未发生的系统状态：
 * - send → analyzing：后端正在分析问题（Query Analyzer / 路由决策）
 * - start → searching：正在检索中医知识库
 * - citations → 已找到 X 条相关证据（X = citations.length，来自真实检索结果）
 * - citations ~ 首个 delta 之间：正在整理相关资料
 * - 首个 delta 之后：步骤列表让位于流式正文（不遮挡回答）
 */
function AnswerProcess({
  phase,
  evidenceCount,
  token,
}: {
  phase: NonNullable<ChatMessage['streamPhase']>;
  evidenceCount: number;
  token: ReturnType<typeof theme.useToken>['token'];
}) {
  const found = phase === 'generating' ? evidenceCount : 0;
  const steps: { label: string; state: 'done' | 'active' | 'pending' }[] = [
    { label: '正在分析问题', state: phase === 'analyzing' ? 'active' : 'done' },
    { label: '正在检索中医知识库', state: phase === 'searching' ? 'active' : 'done' },
    {
      label: found > 0 ? `已找到 ${found} 条相关证据` : '正在查找相关证据',
      state: phase === 'generating' ? 'done' : 'pending',
    },
    { label: '正在整理相关资料', state: phase === 'generating' ? 'active' : 'pending' },
  ];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '4px 0' }}>
      {steps.map((step) => (
        <div
          key={step.label}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontSize: 13,
            color:
              step.state === 'pending'
                ? token.colorTextQuaternary
                : step.state === 'done'
                  ? token.colorTextSecondary
                  : token.colorText,
          }}
        >
          {step.state === 'done' ? (
            <CheckCircleOutlined style={{ color: token.colorSuccess, fontSize: 14 }} />
          ) : step.state === 'active' ? (
            <LoadingOutlined spin style={{ color: token.colorPrimary, fontSize: 14 }} />
          ) : (
            <span style={{ width: 14, textAlign: 'center', fontSize: 12 }}>·</span>
          )}
          {step.label}
          {step.state === 'active' && <ThinkingDots color={token.colorTextTertiary} />}
        </div>
      ))}
    </div>
  );
}

/**
 * 将回答正文渲染为"文本 + 内联引用 chip"（引用 0 修复）。
 *
 * - chip 恒显示 [N]（与 Evidence Panel 的来源编号完全一致）；页码只进 tooltip，
 *   不再把模型填的页码 0 当编号显示（旧 bug：`[citation: 1, 0]` 显示成 0）；
 * - 编号映射失败（0 / 越界 / 无对应证据卡片）→ 降级为普通文本（不可点击），
 *   console.warn 记录，绝不显示错误的编号；
 * - 点击 chip 展开 / 收起对应证据面板（key = source_index）。
 */
function renderAnswer(
  content: string,
  onChip: (index: number) => void,
  sourceIndexes: number[],
  token: ReturnType<typeof theme.useToken>['token'],
): ReactNode[] {
  const nodes: ReactNode[] = [];
  const segments = parseCitationMarkers(content);
  let key = 0;
  for (const seg of segments) {
    if (seg.type === 'text') {
      if (seg.text) nodes.push(<span key={`t${key++}`}>{seg.text}</span>);
      continue;
    }
    const { index, page } = seg;
    // 映射失败：无对应证据卡片 → 渲染为普通文本（不显示错误编号、不可点击）
    if (!resolveCitationKey(index, sourceIndexes)) {
      if (typeof console !== 'undefined') {
        console.warn(
          `[citation] 无效引用编号 ${index}（可用编号：${sourceIndexes.join(', ') || '无'}），已按普通文本显示`,
        );
      }
      nodes.push(<span key={`i${key++}`}>{`[${index}]`}</span>);
      continue;
    }
    const title =
      page && page > 0 ? `查看引用来源 ${index}（第 ${page} 页）` : `查看引用来源 ${index}`;
    nodes.push(
      <sup
        key={`c${key++}`}
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
          background: token.colorPrimaryBg,
          color: token.colorPrimary,
          border: `1px solid ${token.colorPrimaryBorder}`,
          fontSize: 12,
          cursor: 'pointer',
        }}
        title={title}
      >
        {`[${index}]`}
      </sup>,
    );
  }
  return nodes;
}

export function MessageItem({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user';
  const [activeKey, setActiveKey] = useState<string[]>([]);
  // 模块 E：视觉 token 化（修复暗色模式下气泡硬编码白底/浅色文字的问题）
  const { token } = theme.useToken();

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
          style={{ backgroundColor: token.colorPrimary, flexShrink: 0 }}
          icon={<RobotOutlined />}
        />
      )}
      <div style={{ maxWidth: '78%' }}>
        <div
          style={{
            padding: '12px 16px',
            borderRadius: 16,
            background: isUser ? token.colorPrimary : token.colorBgContainer,
            color: isUser ? token.colorWhite : token.colorText,
            boxShadow: token.boxShadowTertiary,
            border: isUser ? 'none' : `1px solid ${token.colorBorderSecondary}`,
          }}
        >
          {isUser ? (
            <Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', color: token.colorWhite }}>
              {message.content}
            </Paragraph>
          ) : message.streamPhase && !message.content ? (
            // 回答过程步骤（问题8/9）：分析 → 检索 → 已找到 X 条证据 → 整理资料；
            // 首个 delta 到达后让位于流式正文，不遮挡回答
            <AnswerProcess
              phase={message.streamPhase}
              evidenceCount={message.citations?.length ?? 0}
              token={token}
            />
          ) : (
            <Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
              {/* BUG-053：历史脏数据 content=null 会让 renderAnswer 直接抛错（白屏） */}
              {renderAnswer(message.content ?? '', onChip, sourceIndexes, token)}
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
            onChange={(keys) => setActiveKey(keys as string[])}
          />
        )}
      </div>
      {isUser && <Avatar size={32} style={{ flexShrink: 0 }} icon={<UserOutlined />} />}
    </div>
  );
}
