'use client';

import { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
  type UploadProps,
} from 'antd';
import {
  CloudUploadOutlined,
  DeleteOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { RcFile, UploadFile } from 'antd/es/upload';
import type { DocumentItem, ParseStatus, SourceEra, SourceType } from '@/types';
import { uploadDocument } from '@/services/api';
import { isParsing, useDocumentStore } from '@/stores/documentStore';
import {
  CREDIBILITY_MAP,
  SOURCE_ERA_OPTIONS,
  SOURCE_TYPE_OPTIONS,
  credibilityColor,
} from '@/constants/source';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';

const { Text } = Typography;

const STATUS_MAP: Record<ParseStatus, { label: string; color: string }> = {
  pending: { label: '待解析', color: 'default' },
  parsing: { label: '解析中', color: 'processing' },
  success: { label: '切片就绪', color: 'gold' },
  completed: { label: '已向量化', color: 'success' },
  failed: { label: '失败', color: 'error' },
};

/** 字节 → 友好显示。 */
function formatBytes(bytes: number): string {
  if (!bytes) return '-';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/** ISO 时间 → 本地显示。 */
function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN', { hour12: false });
}

/** 允许上传的文档类型（与后端 DocumentService 一致）。 */
const ACCEPT = '.pdf,.docx,.txt,.md';

export default function DocumentsPage() {
  const {
    knowledgeBases,
    selectedKbId,
    loadingKbs,
    selectKb,
    loadKnowledgeBases,
    documents,
    loading,
    loadDocuments,
    removeDocument,
    reparse,
    reindex,
    error,
    clearError,
  } = useDocumentStore();
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  // 本批上传的来源标注（作用于随后选择的所有文件；credibility 只读、由类型带出）
  const [sourceType, setSourceType] = useState<SourceType | null>(null);
  const [era, setEra] = useState<SourceEra | null>(null);
  const credibility = sourceType ? CREDIBILITY_MAP[sourceType] : null;

  // 初始化：加载知识库，并按 URL ?kb_id= 预选（来自「管理文档」跳转）
  useEffect(() => {
    const qKb = new URLSearchParams(window.location.search).get('kb_id');
    void loadKnowledgeBases().then(() => {
      if (qKb) {
        selectKb(qKb);
      } else {
        void loadDocuments();
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 解析中自动轮询：任一文档 pending/parsing 时每 4s 刷新
  const needPoll = documents.some((d) => isParsing(d.parse_status));
  useEffect(() => {
    if (!needPoll) return;
    const t = setInterval(() => void loadDocuments(), 4_000);
    return () => clearInterval(t);
  }, [needPoll, loadDocuments]);

  // 批次上传全部完成（done/error）后，清空列表并刷新表格
  useEffect(() => {
    if (fileList.length === 0) return;
    const allDone = fileList.every((f) => f.status === 'done' || f.status === 'error');
    if (allDone) {
      const failed = fileList.filter((f) => f.status === 'error').length;
      const ok = fileList.length - failed;
      if (ok > 0) message.success(`${ok} 个文档上传成功，正在后台解析`);
      if (failed > 0) message.error(`${failed} 个文档上传失败`);
      const timer = setTimeout(() => {
        setFileList([]);
        void loadDocuments();
      }, 900);
      return () => clearTimeout(timer);
    }
  }, [fileList, loadDocuments]);

  // antd Upload 逐文件上传：走统一 API 客户端，获得与表格一致的错误处理
  const handleUpload: UploadProps['customRequest'] = async (options) => {
    const { file, onSuccess, onError, onProgress } = options;
    if (!selectedKbId) {
      onError?.(new Error('未选择知识库'));
      return;
    }
    try {
      await uploadDocument(
        file as RcFile,
        selectedKbId,
        (p) => onProgress?.({ percent: p }),
        { sourceType, era },
      );
      onSuccess?.({}, file);
    } catch (err) {
      onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  };

  const columns: ColumnsType<DocumentItem> = [
    {
      title: '文档名',
      dataIndex: 'file_name',
      key: 'file_name',
      ellipsis: true,
    },
    {
      title: '格式',
      dataIndex: 'file_type',
      key: 'file_type',
      width: 80,
      render: (t: string) => t.toUpperCase(),
    },
    {
      title: '来源类型',
      dataIndex: 'source_type',
      key: 'source_type',
      width: 100,
      render: (t?: SourceType | null) =>
        t ? <Tag color={credibilityColor(CREDIBILITY_MAP[t])}>{t}</Tag> : <Text type="secondary">-</Text>,
    },
    {
      title: '年代',
      dataIndex: 'era',
      key: 'era',
      width: 70,
      render: (t?: SourceEra | null) => t ?? '-',
    },
    {
      title: '可信度',
      dataIndex: 'credibility_level',
      key: 'credibility_level',
      width: 90,
      render: (lv?: number | null) =>
        lv ? <Tag color={credibilityColor(lv)}>Lv{lv}</Tag> : <Text type="secondary">-</Text>,
    },
    {
      title: '大小',
      dataIndex: 'file_size',
      key: 'file_size',
      width: 100,
      render: (s: number) => formatBytes(s),
    },
    {
      title: '解析状态',
      dataIndex: 'parse_status',
      key: 'parse_status',
      width: 120,
      render: (s: ParseStatus, row) => {
        const tag = <Tag color={STATUS_MAP[s]?.color}>{STATUS_MAP[s]?.label ?? s}</Tag>;
        return row.error_message ? (
          <Tooltip title={row.error_message}>
            <span>{tag}</span>
          </Tooltip>
        ) : (
          tag
        );
      },
    },
    { title: '切片数', dataIndex: 'chunk_count', key: 'chunk_count', width: 90 },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (t: string) => formatTime(t),
    },
    {
      title: '操作',
      key: 'action',
      width: 220,
      render: (_, row) => {
        const canReindex = row.parse_status === 'success' || row.parse_status === 'completed';
        return (
          <Space size={4}>
            <Tooltip title={isParsing(row.parse_status) ? '解析进行中' : '重新解析'}>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                disabled={isParsing(row.parse_status)}
                onClick={() => void reparse(row.id)}
              >
                重新解析
              </Button>
            </Tooltip>
            <Tooltip title={canReindex ? '重新向量化' : '需先完成解析'}>
              <Button
                size="small"
                icon={<ThunderboltOutlined />}
                disabled={!canReindex}
                onClick={() => void reindex(row.id)}
              >
                重新向量化
              </Button>
            </Tooltip>
            <Popconfirm
              title="确认删除该文档？"
              description="将同时删除其切片与向量数据，不可恢复。"
              okText="删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={() => void removeDocument(row.id)}
            >
              <Button size="small" danger icon={<DeleteOutlined />}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        );
      },
    },
  ];

  const headerLeft = <h2 style={{ margin: 0 }}>文档管理</h2>;
  const headerRight = (
    <Space size="middle" align="center" wrap>
      <Select
        size="middle"
        style={{ width: 200 }}
        placeholder="选择知识库筛选"
        value={selectedKbId ?? undefined}
        loading={loadingKbs}
        onChange={selectKb}
        options={knowledgeBases.map((kb) => ({ label: kb.name, value: kb.id }))}
        notFoundContent="暂无知识库"
      />
      <Tooltip title="应用于本次选择上传的全部文件；留空则不标注（历史文档可后续补标）">
        <Select
          size="middle"
          style={{ width: 120 }}
          placeholder="来源类型"
          allowClear
          value={sourceType ?? undefined}
          onChange={(v) => setSourceType((v as SourceType) ?? null)}
          options={SOURCE_TYPE_OPTIONS}
        />
      </Tooltip>
      <Select
        size="middle"
        style={{ width: 80 }}
        placeholder="年代"
        allowClear
        value={era ?? undefined}
        onChange={(v) => setEra((v as SourceEra) ?? null)}
        options={SOURCE_ERA_OPTIONS}
      />
      <Tooltip title="可信度等级由来源类型按固定映射自动带出，不可手工修改">
        <Tag
          color={credibilityColor(credibility)}
          style={{ margin: 0, minWidth: 52, textAlign: 'center' }}
        >
          {credibility ? `可信度 Lv${credibility}` : '可信度 -'}
        </Tag>
      </Tooltip>
      <Upload
        fileList={fileList}
        multiple
        accept={ACCEPT}
        showUploadList
        customRequest={handleUpload}
        disabled={!selectedKbId}
        onChange={({ fileList: fl }) => setFileList(fl)}
      >
        <Tooltip title={!selectedKbId ? '请先选择知识库' : '支持 PDF / DOCX / TXT / MD，可多选'}>
          <Button type="primary" icon={<CloudUploadOutlined />} disabled={!selectedKbId}>
            上传文档
          </Button>
        </Tooltip>
      </Upload>
      <AdminHeaderRight />
    </Space>
  );

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      {error && (
        <Alert
          type="error"
          message={error}
          closable
          showIcon
          style={{ marginBottom: 16 }}
          onClose={clearError}
        />
      )}

      <Table
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={documents}
        loading={loading}
        pagination={{ pageSize: 10, showSizeChanger: true }}
        locale={{ emptyText: selectedKbId ? '该知识库暂无文档' : '请选择知识库查看文档' }}
      />
    </AppLayout>
  );
}
