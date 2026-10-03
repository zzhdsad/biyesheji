'use client';

import type { ComponentType, CSSProperties } from 'react';
import { Card, Col, Input, Row, Space, Tag, Typography, theme } from 'antd';
import {
  BookOutlined,
  MedicineBoxOutlined,
  ProfileOutlined,
  ReadOutlined,
  SendOutlined,
} from '@ant-design/icons';
import { useRouter } from 'next/navigation';

const { Text, Title, Paragraph } = Typography;

/** 与后端 chat.py `ChatAskRequest.question` 的 max_length 一致（BUG-052）。 */
const QUESTION_MAX_LENGTH = 2000;

/** 示例问题卡片：按知识资源类别组织，引导非医学生用户开始提问。 */
const EXAMPLE_QUESTIONS: {
  category: string;
  question: string;
}[] = [
  { category: '中药', question: '丹参有什么功效？' },
  { category: '方剂', question: '桂枝汤适用于什么证型？' },
  { category: '中医理论', question: '什么是卫气？' },
  { category: '文献', question: '《伤寒论》中如何论述桂枝汤？' },
];

/** 轻量知识入口：跳转到对应的知识资源浏览页。 */
const KNOWLEDGE_ENTRIES: {
  key: string;
  title: string;
  desc: string;
  icon: ComponentType<{ style?: CSSProperties }>;
  path: string;
}[] = [
  {
    key: 'herbs',
    title: '中药',
    desc: '性味归经 · 功效主治',
    icon: MedicineBoxOutlined,
    path: '/herbs',
  },
  {
    key: 'prescriptions',
    title: '方剂',
    desc: '经典方剂 · 组成用法',
    icon: ProfileOutlined,
    path: '/prescriptions',
  },
  {
    key: 'theories',
    title: '中医理论',
    desc: '基础理论 · 系统学习',
    icon: ReadOutlined,
    path: '/theories',
  },
  {
    key: 'literatures',
    title: '文献',
    desc: '历代典籍 · 原文检索',
    icon: BookOutlined,
    path: '/literatures',
  },
];

/**
 * /chat 空状态：AI 助手欢迎界面（问题二、三）。
 *
 * 当前会话没有任何消息时渲染，替代后台风格的 Empty 组件：
 * 视觉重心居中（主标题 → 副标题 → 中央输入框 → 知识范围 → 示例问题 →
 * 知识入口）。一旦发出第一条消息，chat 页切换回正常聊天布局，
 * 欢迎界面与聊天界面互斥呈现。
 *
 * 交互约定（不自动发送）：
 * - 中央输入框与聊天页共用同一 input state，回车 / 点「提问」才发送；
 * - 示例问题卡片只把问题**填入**输入框，由用户确认后发送。
 */
export function ChatWelcome({
  value,
  onChange,
  onSend,
  disabled,
  scopeLabel,
  onPickExample,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  /** 无可用知识库时禁用输入（与聊天页 footer 行为一致）。 */
  disabled: boolean;
  /** 当前知识范围展示文案（"全部知识库" / "已选 N 个知识库"）。 */
  scopeLabel: string;
  /** 点击示例问题：填入输入框，不自动发送。 */
  onPickExample: (question: string) => void;
}) {
  const router = useRouter();
  const { token } = theme.useToken();

  return (
    <div
      style={{
        minHeight: 'calc(100vh - 160px)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '24px 16px',
      }}
    >
      <div style={{ width: '100%', maxWidth: 760 }}>
        {/* 主标题 + 副标题 */}
        <div style={{ textAlign: 'center', marginBottom: 28 }}>
          <div
            style={{
              width: 56,
              height: 56,
              borderRadius: token.borderRadiusLG,
              background: token.colorPrimaryBg,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              margin: '0 auto 16px',
            }}
          >
            <MedicineBoxOutlined style={{ fontSize: 28, color: token.colorPrimary }} />
          </div>
          <Title level={2} style={{ marginBottom: 8, marginTop: 0 }}>
            你好，我是中医知识助手
          </Title>
          <Paragraph
            type="secondary"
            style={{ fontSize: 15, maxWidth: 520, margin: '0 auto' }}
          >
            基于中医知识库，为你检索、整理并解释中医知识
          </Paragraph>
        </div>

        {/* 中央大输入框（与聊天页共用 input state，回车 / 提问按钮才发送） */}
        <Input.TextArea
          size="large"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onPressEnter={(e) => {
            if (!e.shiftKey) {
              e.preventDefault();
              onSend();
            }
          }}
          placeholder="请输入你想了解的中医问题，例如：丹参有什么功效？"
          autoSize={{ minRows: 2, maxRows: 6 }}
          disabled={disabled}
          maxLength={QUESTION_MAX_LENGTH}
          showCount
          style={{
            borderRadius: token.borderRadiusLG,
            padding: '10px 14px',
            fontSize: 15,
          }}
        />
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginTop: 12,
            flexWrap: 'wrap',
            gap: 8,
          }}
        >
          <Space size={8}>
            <Text type="secondary" style={{ fontSize: 13 }}>
              知识范围：
            </Text>
            <Tag color="blue" style={{ margin: 0 }}>
              {scopeLabel}
            </Tag>
          </Space>
          <Space size={8}>
            {disabled && (
              <Text type="warning" style={{ fontSize: 13 }}>
                暂无可用知识库，请先创建或上传文档
              </Text>
            )}
            <button
              type="button"
              onClick={onSend}
              disabled={disabled || !value.trim()}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                border: 'none',
                cursor: disabled || !value.trim() ? 'not-allowed' : 'pointer',
                padding: '6px 20px',
                borderRadius: token.borderRadius,
                fontSize: 14,
                fontWeight: 500,
                color: token.colorWhite,
                background:
                  disabled || !value.trim() ? token.colorBgContainer : token.colorPrimary,
                boxShadow: token.boxShadowTertiary,
                opacity: disabled || !value.trim() ? 0.6 : 1,
              }}
            >
              <SendOutlined /> 提问
            </button>
          </Space>
        </div>

        {/* 示例问题卡片：点击填入输入框（不自动发送） */}
        <div style={{ marginTop: 32, marginBottom: 8 }}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            试着这样问
          </Text>
        </div>
        <Row gutter={[12, 12]}>
          {EXAMPLE_QUESTIONS.map((ex) => (
            <Col xs={24} sm={12} key={ex.question}>
              <Card
                size="small"
                hoverable
                onClick={() => onPickExample(ex.question)}
                style={{ borderRadius: token.borderRadiusLG, height: '100%' }}
              >
                <Space direction="vertical" size={4}>
                  <Tag
                    color="geekblue"
                    style={{ margin: 0, fontSize: 12, lineHeight: '18px' }}
                  >
                    {ex.category}
                  </Tag>
                  <Text style={{ fontSize: 14 }}>{ex.question}</Text>
                </Space>
              </Card>
            </Col>
          ))}
        </Row>

        {/* 轻量知识入口 */}
        <div style={{ marginTop: 24, marginBottom: 8 }}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            或先浏览知识资源
          </Text>
        </div>
        <Row gutter={[12, 12]}>
          {KNOWLEDGE_ENTRIES.map((entry) => (
            <Col xs={12} md={6} key={entry.key}>
              <Card
                size="small"
                hoverable
                onClick={() => router.push(entry.path)}
                style={{ borderRadius: token.borderRadiusLG }}
              >
                <Space size={10} align="center">
                  <entry.icon style={{ fontSize: 20, color: token.colorPrimary }} />
                  <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1.3 }}>
                    <Text strong style={{ fontSize: 14 }}>
                      {entry.title}
                    </Text>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {entry.desc}
                    </Text>
                  </div>
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      </div>
    </div>
  );
}
