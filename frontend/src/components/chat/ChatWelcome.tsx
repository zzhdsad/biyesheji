'use client';

import { Input, Space, Tag, Typography, theme } from 'antd';
import { BulbOutlined, SendOutlined } from '@ant-design/icons';

const { Text, Title, Paragraph } = Typography;

/** 与后端 chat.py `ChatAskRequest.question` 的 max_length 一致（BUG-052）。 */
const QUESTION_MAX_LENGTH = 2000;

/**
 * /chat 空状态：AI 助手欢迎界面（问题二、三）。
 *
 * 当前会话没有任何消息时渲染，替代后台风格的 Empty 组件：
 * 视觉重心居中（主标题 → 副标题 → 中央输入框 → 知识范围）。
 * 一旦发出第一条消息，chat 页切换回正常聊天布局，欢迎界面与聊天界面互斥呈现。
 *
 * 交互约定（不自动发送）：中央输入框与聊天页共用同一 input state，
 * 回车 / 点「提问」才发送。
 */
export function ChatWelcome({
  value,
  onChange,
  onSend,
  disabled,
  disabledHint,
  scopeLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  /** 无可用知识库时禁用输入（与聊天页 footer 行为一致）。 */
  disabled: boolean;
  /** 禁用时的提示文案（问题3：员工无知识库权限提示）。 */
  disabledHint?: string;
  /** 当前知识范围展示文案（"全部知识库" / "已选 N 个知识库"）。 */
  scopeLabel: string;
}) {
  const { token } = theme.useToken();

  return (
    <div
      className="chat-welcome"
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
            className="welcome-orb"
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
            <BulbOutlined style={{ fontSize: 28, color: token.colorPrimary }} />
          </div>
          <Title level={2} className="welcome-title" style={{ marginBottom: 8, marginTop: 0 }}>
            你好，我是知识问答助手
          </Title>
          <Paragraph
            type="secondary"
            style={{ fontSize: 15, maxWidth: 520, margin: '0 auto' }}
          >
            基于你的知识库，为你检索、整理并解答问题
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
          placeholder="请输入你的问题，例如：这份资料的核心观点是什么？"
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
            <Tag color="cyan" style={{ margin: 0 }}>
              {scopeLabel}
            </Tag>
          </Space>
          <Space size={8}>
            {disabled && (
              <Text type="warning" style={{ fontSize: 13 }}>
                {disabledHint || '暂无可用知识库，请先创建或上传文档'}
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
                cursor: disabled || !value.trim() ? 'not-allowed' : 'pointer',
                padding: '6px 20px',
                borderRadius: token.borderRadius,
                fontSize: 14,
                fontWeight: 500,
                color: disabled || !value.trim() ? token.colorTextSecondary : token.colorWhite,
                background:
                  disabled || !value.trim() ? token.colorFillTertiary : token.colorPrimary,
                border:
                  disabled || !value.trim()
                    ? `1px solid ${token.colorBorder}`
                    : '1px solid transparent',
                boxShadow: token.boxShadowTertiary,
                opacity: disabled || !value.trim() ? 0.6 : 1,
              }}
            >
              <SendOutlined /> 提问
            </button>
          </Space>
        </div>
      </div>
    </div>
  );
}
