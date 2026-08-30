'use client';

import { useState } from 'react';
import { Button, Space, Table, Tag, Upload } from 'antd';
import { ReloadOutlined, UploadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { DocumentItem, ParseStatus } from '@/types';

const STATUS_MAP: Record<ParseStatus, { label: string; color: string }> = {
  pending: { label: '待解析', color: 'default' },
  parsing: { label: '解析中', color: 'processing' },
  success: { label: '成功', color: 'success' },
  failed: { label: '失败', color: 'error' },
};

// TODO: 从 GET /api/v1/documents 获取真实数据
const MOCK_DATA: DocumentItem[] = [
  {
    id: '1',
    kb_id: '1',
    file_name: '员工手册.pdf',
    file_type: 'pdf',
    file_size: 2_411_520,
    parse_status: 'success',
    chunk_count: 58,
    created_at: '2026-08-29 10:00',
  },
  {
    id: '2',
    kb_id: '1',
    file_name: '报销制度.docx',
    file_type: 'docx',
    file_size: 156_672,
    parse_status: 'parsing',
    chunk_count: 0,
    created_at: '2026-08-29 11:20',
  },
  {
    id: '3',
    kb_id: '2',
    file_name: '架构设计.md',
    file_type: 'md',
    file_size: 34_816,
    parse_status: 'failed',
    chunk_count: 0,
    created_at: '2026-08-28 16:45',
  },
];

const columns: ColumnsType<DocumentItem> = [
  { title: '文档名', dataIndex: 'file_name', key: 'file_name' },
  {
    title: '格式',
    dataIndex: 'file_type',
    key: 'file_type',
    width: 80,
    render: (t: string) => t.toUpperCase(),
  },
  {
    title: '解析状态',
    dataIndex: 'parse_status',
    key: 'parse_status',
    width: 110,
    render: (s: ParseStatus) => <Tag color={STATUS_MAP[s]?.color}>{STATUS_MAP[s]?.label ?? s}</Tag>,
  },
  { title: '切片数', dataIndex: 'chunk_count', key: 'chunk_count', width: 90 },
  { title: '上传时间', dataIndex: 'created_at', key: 'created_at', width: 160 },
  {
    title: '操作',
    key: 'action',
    width: 200,
    render: () => (
      <Space>
        <Button size="small" icon={<ReloadOutlined />}>
          重新向量化
        </Button>
        <Button size="small" danger>
          删除
        </Button>
      </Space>
    ),
  },
];

export default function DocumentsPage() {
  const [data] = useState<DocumentItem[]>(MOCK_DATA);

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>文档管理</h2>
        {/* TODO: 上传前需选择目标知识库；对接 POST /api/v1/documents/upload */}
        <Upload action="/api/v1/documents/upload" data={{ kb_id: '1' }} showUploadList={false}>
          <Button type="primary" icon={<UploadOutlined />}>
            上传文档
          </Button>
        </Upload>
      </div>
      <Table columns={columns} dataSource={data} rowKey="id" pagination={{ pageSize: 10 }} />
    </div>
  );
}
