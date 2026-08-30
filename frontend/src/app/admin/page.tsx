'use client';

import { Card, Col, Row, Statistic } from 'antd';
import {
  DatabaseOutlined,
  FileTextOutlined,
  MessageOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';

// TODO: 从统计接口获取真实数据
export default function AdminPage() {
  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginTop: 0 }}>系统仪表盘</h2>
      <Row gutter={[16, 16]}>
        <Col xs={12} lg={6}>
          <Card>
            <Statistic title="文档总数" value={55} prefix={<FileTextOutlined />} />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card>
            <Statistic title="知识库数量" value={2} prefix={<DatabaseOutlined />} />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card>
            <Statistic title="累计问答数" value={1320} prefix={<MessageOutlined />} />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card>
            <Statistic
              title="平均响应时间"
              value={1.8}
              suffix="s"
              precision={1}
              prefix={<ThunderboltOutlined />}
            />
          </Card>
        </Col>
      </Row>

      <Card style={{ marginTop: 16 }} title="评估面板">
        评估面板建设中：上传测试集（问题-标准答案-上下文）后可运行 RAGAS 评估，
        展示上下文相关度与答案正确率。
      </Card>
    </div>
  );
}
