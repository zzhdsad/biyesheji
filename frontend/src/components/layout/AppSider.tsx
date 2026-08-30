'use client';

import { Button, Divider, Layout, List, Typography } from 'antd';
import { DatabaseOutlined, HistoryOutlined, PlusOutlined } from '@ant-design/icons';

const { Sider } = Layout;

// TODO: 从 GET /api/v1/kb 与 GET /api/v1/chat/conversations 获取真实数据
const MOCK_KBS = [
  { id: '1', name: '公司制度', docCount: 12 },
  { id: '2', name: '技术文档', docCount: 35 },
  { id: '3', name: '项目案例', docCount: 8 },
];

const MOCK_CONVERSATIONS = ['入职流程咨询', '报销标准问答', 'API 网关配置'];

/** 左侧边栏：知识库列表 + 历史会话（PRD 5.1）。 */
export function AppSider() {
  return (
    <Sider
      width={260}
      theme="light"
      style={{ borderRight: '1px solid #f0f0f0', padding: 16, overflow: 'auto' }}
    >
      <Button type="dashed" icon={<PlusOutlined />} block>
        新建会话
      </Button>

      <Divider orientation="left" plain>
        <DatabaseOutlined /> 知识库
      </Divider>
      <List
        size="small"
        dataSource={MOCK_KBS}
        renderItem={(kb) => (
          <List.Item style={{ cursor: 'pointer' }}>
            <Typography.Text ellipsis style={{ maxWidth: 150 }}>
              {kb.name}
            </Typography.Text>
            <Typography.Text type="secondary">{kb.docCount} 篇</Typography.Text>
          </List.Item>
        )}
      />

      <Divider orientation="left" plain>
        <HistoryOutlined /> 历史会话
      </Divider>
      <List
        size="small"
        dataSource={MOCK_CONVERSATIONS}
        renderItem={(title) => (
          <List.Item style={{ cursor: 'pointer' }}>
            <Typography.Text ellipsis style={{ maxWidth: 190 }}>
              {title}
            </Typography.Text>
          </List.Item>
        )}
      />
    </Sider>
  );
}
