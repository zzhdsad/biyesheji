'use client';

import { useEffect, useState } from 'react';
import {
  Alert,
  Card,
  DatePicker,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { AuditLog } from '@/types';
import { fetchAuditLogs } from '@/services/api';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';

const { Title, Text } = Typography;

/** 简单日期格式化（避免额外依赖 dayjs）。 */
function fmtDate(v: string): string {
  try {
    const d = new Date(v);
    const pad = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch {
    return v || '-';
  }
}

const OPERATION_LABELS: Record<string, string> = {
  create: '创建',
  update: '更新',
  delete: '删除',
  disable: '禁用',
  enable: '启用',
  restore: '恢复',
  purge: '彻底删除',
  login: '登录',
  add_member: '添加成员',
  remove_member: '移除成员',
  batch_add_members: '批量添加成员',
  update_member_role: '修改成员角色',
  transfer_ownership: '转移所有权',
};

const TARGET_LABELS: Record<string, string> = {
  user: '用户',
  kb: '知识库',
  document: '文档',
  message: '消息',
  config: '配置',
};

const OPERATION_COLORS: Record<string, string> = {
  create: 'green',
  update: 'blue',
  delete: 'orange',
  disable: 'volcano',
  enable: 'cyan',
  restore: 'lime',
  purge: 'red',
  login: 'purple',
};

export default function AuditPage() {
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [operationFilter, setOperationFilter] = useState<string | undefined>();
  const [targetFilter, setTargetFilter] = useState<string | undefined>();

  const loadLogs = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchAuditLogs({
        operation: operationFilter,
        target_type: targetFilter,
        limit: 200,
      });
      setLogs(data);
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e?.message || '加载失败';
      setError(typeof msg === 'string' ? msg : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadLogs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [operationFilter, targetFilter]);

  const columns: ColumnsType<AuditLog> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v: string) => (v ? fmtDate(v) : '-'),
    },
    {
      title: '操作人',
      dataIndex: 'operator_name',
      width: 120,
    },
    {
      title: '操作类型',
      dataIndex: 'operation',
      width: 120,
      render: (v: string) => (
        <Tag color={OPERATION_COLORS[v] || 'default'}>
          {OPERATION_LABELS[v] || v}
        </Tag>
      ),
    },
    {
      title: '目标类型',
      dataIndex: 'target_type',
      width: 100,
      render: (v: string) => TARGET_LABELS[v] || v,
    },
    {
      title: '目标ID',
      dataIndex: 'target_id',
      width: 280,
      render: (v: string) => (
        <Text copyable={{ text: v }} style={{ fontSize: 12 }}>
          {v ? v.slice(0, 12) + '...' : '-'}
        </Text>
      ),
    },
    {
      title: '详情',
      dataIndex: 'detail',
      render: (v: Record<string, unknown>) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {Object.keys(v).length > 0 ? JSON.stringify(v) : '-'}
        </Text>
      ),
    },
    {
      title: 'IP',
      dataIndex: 'ip',
      width: 140,
    },
  ];

  return (
    <AppLayout headerRight={<AdminHeaderRight />}>
      <Card>
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Title level={4}>审计日志</Title>
          {error && <Alert type="error" message={error} closable onClose={() => setError(null)} />}
          <Space>
            <Select
              placeholder="操作类型"
              allowClear
              style={{ width: 150 }}
              value={operationFilter}
              onChange={setOperationFilter}
              options={Object.entries(OPERATION_LABELS).map(([value, label]) => ({ value, label }))}
            />
            <Select
              placeholder="目标类型"
              allowClear
              style={{ width: 150 }}
              value={targetFilter}
              onChange={setTargetFilter}
              options={Object.entries(TARGET_LABELS).map(([value, label]) => ({ value, label }))}
            />
          </Space>
          <Table
            columns={columns}
            dataSource={logs}
            rowKey="id"
            loading={loading}
            size="small"
            pagination={{ pageSize: 20, showSizeChanger: true }}
            scroll={{ x: 900 }}
          />
        </Space>
      </Card>
    </AppLayout>
  );
}
