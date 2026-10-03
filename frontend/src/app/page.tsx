'use client';

import { useEffect, useState, type ComponentType, type CSSProperties } from 'react';
import { Card, Col, Input, Row, Space, Typography, theme } from 'antd';
import {
  ArrowRightOutlined,
  BookOutlined,
  DatabaseOutlined,
  MedicineBoxOutlined,
  MessageOutlined,
  ProfileOutlined,
  ReadOutlined,
  SendOutlined,
} from '@ant-design/icons';
import { useRouter } from 'next/navigation';
import { AppLayout } from '@/components/layout/AppLayout';
import { ChatHeaderRight } from '@/components/layout/AppSider';
import { useChatStore } from '@/stores/chatStore';

/** 与后端 chat.py `ChatAskRequest.question` 的 max_length 一致（BUG-052）。 */
const QUESTION_MAX_LENGTH = 2000;

/** 示例问题：轮播展示在首页输入框 placeholder，引导非医学生用户提问。 */
const EXAMPLE_QUESTIONS = [
  '请输入中医相关问题，例如：丹参有什么功效？',
  '桂枝汤适用于什么证型？',
  '金银花的性味归经是什么？',
  '小柴胡汤由哪些中药组成？',
  '「太阳病」指的是什么？',
];

/**
 * 首页 / 工作台：面向中医药学生的系统入口。
 *
 * 信息架构原则：一个页面突出一个主要任务——这里只做三件事：
 * 1. 告诉用户这个系统是什么（一句话简介）；
 * 2. 把用户送到最核心的入口（智能问答）；
 * 3. 提供知识资源的浏览入口与最近会话的快速继续。
 * 不堆统计图、不暴露管理功能（管理中心入口在侧边栏，仅 admin 可见）。
 */
export default function HomePage() {
  const router = useRouter();
  const { token } = theme.useToken();
  const { conversations, loadConversations, knowledgeBases, loadKnowledgeBases } = useChatStore();

  useEffect(() => {
    void loadKnowledgeBases();
    void loadConversations();
  }, [loadKnowledgeBases, loadConversations]);

  // 模块 C：首页即问。输入后带 ?q= 跳转问答页（问答页预填、不自动发送）
  const [question, setQuestion] = useState('');
  const [exampleIndex, setExampleIndex] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => {
      setExampleIndex((i) => (i + 1) % EXAMPLE_QUESTIONS.length);
    }, 4000);
    return () => clearInterval(timer);
  }, []);

  const goAsk = (text: string) => {
    const trimmed = text.trim();
    router.push(trimmed ? `/chat?q=${encodeURIComponent(trimmed)}` : '/chat');
  };

  const recent = conversations.slice(0, 5);

  const entries: {
    key: string;
    title: string;
    desc: string;
    icon: ComponentType<{ style?: CSSProperties }>;
    path: string;
  }[] = [
    {
      key: 'herbs',
      title: '中药',
      desc: '查阅中药性味归经与功效',
      icon: MedicineBoxOutlined,
      path: '/herbs',
    },
    {
      key: 'prescriptions',
      title: '方剂',
      desc: '浏览经典方剂的组成与主治',
      icon: ProfileOutlined,
      path: '/prescriptions',
    },
    {
      key: 'theories',
      title: '中医理论',
      desc: '系统学习中医基础理论',
      icon: ReadOutlined,
      path: '/theories',
    },
    {
      key: 'literatures',
      title: '文献',
      desc: '检索历代中医文献资料',
      icon: BookOutlined,
      path: '/literatures',
    },
  ];

  const content = (
    <div style={{ maxWidth: 1000, margin: '0 auto', padding: '24px 0' }}>
      {/* 主任务：开始提问 */}
      <Card
        style={{
          textAlign: 'center',
          borderRadius: 16,
          background: `linear-gradient(135deg, ${token.colorPrimaryBg}, ${token.colorBgContainer})`,
          marginBottom: 24,
        }}
        styles={{ body: { padding: '40px 24px' } }}
      >
        <MessageOutlined style={{ fontSize: 40, color: token.colorPrimary, marginBottom: 12 }} />
        <Typography.Title level={3} style={{ marginBottom: 8 }}>
          你好，我是中医知识助手
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ maxWidth: 560, margin: '0 auto 20px' }}>
          向知识库提问，答案基于中药、方剂、理论与文献资料生成，并附引用来源与证据等级。
        </Typography.Paragraph>
        <Input.Search
          size="large"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onSearch={goAsk}
          placeholder={EXAMPLE_QUESTIONS[exampleIndex]}
          maxLength={QUESTION_MAX_LENGTH}
          enterButton={
            <span>
              <SendOutlined /> 提问
            </span>
          }
          style={{ maxWidth: 640, margin: '0 auto 20px' }}
        />
        <Space size={12} wrap>
          <a onClick={() => goAsk('')} style={{ fontSize: 15, fontWeight: 600 }}>
            进入智能问答 <ArrowRightOutlined />
          </a>
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            {knowledgeBases.length > 0 ? `${knowledgeBases.length} 个知识库可用` : '暂无知识库'}
          </Typography.Text>
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        {/* 知识资源入口 */}
        {entries.map((e) => (
          <Col xs={12} md={6} key={e.key}>
            <Card
              hoverable
              style={{ borderRadius: 12, height: '100%' }}
              onClick={() => router.push(e.path)}
            >
              <Space direction="vertical" size={4}>
                <e.icon style={{ fontSize: 22, color: token.colorPrimary }} />
                <Typography.Text strong>{e.title}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {e.desc}
                </Typography.Text>
              </Space>
            </Card>
          </Col>
        ))}

        {/* 最近会话：继续学习 */}
        <Col span={24}>
          <Card
            title={
              <span>
                <DatabaseOutlined /> 最近问答
              </span>
            }
            style={{ borderRadius: 12 }}
            styles={{ body: { paddingTop: 8 } }}
          >
            {recent.length === 0 ? (
              <Typography.Text type="secondary">
                还没有问答记录，从上面的「开始提问」进入吧。
              </Typography.Text>
            ) : (
              recent.map((conv) => (
                <a
                  key={conv.id}
                  onClick={() => router.push('/chat')}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    padding: '8px 4px',
                    borderBottom: `1px solid ${token.colorBorderSecondary}`,
                  }}
                >
                  <Typography.Text ellipsis style={{ maxWidth: '70%' }}>
                    {conv.title}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    继续对话 <ArrowRightOutlined />
                  </Typography.Text>
                </a>
              ))
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );

  return (
    <AppLayout
      headerLeft={
        <Typography.Title level={4} style={{ margin: 0 }}>
          首页
        </Typography.Title>
      }
      headerRight={<ChatHeaderRight />}
      contentStyle={{ padding: 24, overflow: 'auto' }}
    >
      {content}
    </AppLayout>
  );
}
