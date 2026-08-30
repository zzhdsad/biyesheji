'use client';

import { Button, Card, Col, Empty, Row, Tag, Typography } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import type { KnowledgeBase } from '@/types';

// TODO: 从 GET /api/v1/kb 获取真实数据
const MOCK_KBS: KnowledgeBase[] = [
  {
    id: '1',
    name: '公司制度',
    description: '行政、人事、财务相关制度文档',
    visibility: 'public',
    document_count: 12,
  },
  {
    id: '2',
    name: '技术文档',
    description: '研发规范、架构设计、运维手册',
    visibility: 'private',
    document_count: 35,
  },
];

export default function KBPage() {
  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>知识库管理</h2>
        <Button type="primary" icon={<PlusOutlined />}>
          创建知识库
        </Button>
      </div>
      <Row gutter={[16, 16]}>
        {MOCK_KBS.map((kb) => (
          <Col key={kb.id} xs={24} sm={12} lg={8}>
            <Card
              title={kb.name}
              extra={
                <Tag color={kb.visibility === 'public' ? 'green' : 'gold'}>
                  {kb.visibility === 'public' ? '公开' : '私有'}
                </Tag>
              }
            >
              <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }}>
                {kb.description || '暂无描述'}
              </Typography.Paragraph>
              <Typography.Text type="secondary">文档数：{kb.document_count ?? 0}</Typography.Text>
            </Card>
          </Col>
        ))}
      </Row>
      {MOCK_KBS.length === 0 && <Empty description="暂无知识库" />}
    </div>
  );
}
