'use client';

import { Alert, Button, Card, Col, Row, Space, Statistic } from 'antd';
import {
  DashboardOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  MessageOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { fetchAdminStats } from '@/services/api';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';

/**
 * 系统仪表盘（PRD §5.2）：总文档数 / 知识库数量 / 累计问答数 / 平均响应时间。
 * 数据来自 GET /admin/stats（仅 admin 可访问），useQuery 管理加载/错误/刷新态。
 */
export default function AdminPage() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ['admin', 'stats'],
    queryFn: fetchAdminStats,
    staleTime: 30_000, // 统计数据不需要实时，30s 内不重复请求
    retry: 1,
  });

  const isForbidden =
    isError &&
    (error as { response?: { status?: number } })?.response?.status === 403;

  const headerLeft = (
    <Space size="middle" align="center">
      <DashboardOutlined style={{ fontSize: 20 }} />
      <h2 style={{ margin: 0 }}>系统仪表盘</h2>
    </Space>
  );
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} loading={isFetching} onClick={() => refetch()}>
        刷新
      </Button>
      <AdminHeaderRight />
    </Space>
  );

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      {isForbidden && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="无权访问系统统计"
          description="系统仪表盘仅管理员可见，如有需要请联系管理员开通权限。"
        />
      )}

      {isError && !isForbidden && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="统计数据加载失败"
          description="请确认后端服务与数据库正常运行后点击刷新重试。"
        />
      )}

      <Row gutter={[16, 16]}>
        <Col xs={12} lg={6}>
          <Card loading={isLoading}>
            <Statistic
              title="文档总数"
              value={data?.total_docs ?? 0}
              prefix={<FileTextOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card loading={isLoading}>
            <Statistic
              title="知识库数量"
              value={data?.total_kbs ?? 0}
              prefix={<DatabaseOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card loading={isLoading}>
            <Statistic
              title="累计问答数"
              value={data?.total_qa ?? 0}
              prefix={<MessageOutlined />}
            />
          </Card>
        </Col>
        <Col xs={12} lg={6}>
          <Card loading={isLoading}>
            <Statistic
              title="平均响应时间"
              // 后端返回毫秒；无问答记录时为 0，展示"暂无数据"避免误读为 0.0s
              value={
                data && data.avg_latency_ms > 0
                  ? data.avg_latency_ms / 1000
                  : '暂无数据'
              }
              suffix={data && data.avg_latency_ms > 0 ? 's' : undefined}
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
    </AppLayout>
  );
}
