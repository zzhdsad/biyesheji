'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { CloudSyncOutlined, ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type {
  DocumentItem,
  DocumentProcessingInfo,
  StructuredField,
  StructuredParseInfo,
  StructuredRecordDetail,
  StructuredRecordItem,
} from '@/types';
import {
  fetchDocumentProcessing,
  fetchStructuredParse,
  fetchStructuredRecord,
  fetchStructuredRecords,
  reparseStructured,
  revectorizeStructured,
} from '@/services/api';
import { describeApiError } from '@/utils/apiError';
import {
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  cellValueText,
  fieldTypeLabel,
  structuredParseStatusMeta,
} from '@/utils/structuredDisplay';
import { isTaskInFlight, vectorStatusMeta } from '@/utils/processingStatus';
import { useProcessingOverview } from '@/hooks/useProcessingOverview';
import { ProcessingOverviewPanel } from '@/components/common/ProcessingStatusTag';

interface Props {
  doc: DocumentItem | null;
  open: boolean;
  onClose: () => void;
  /** 重新解析派发后通知列表刷新（列表里的解析状态列随之更新）。 */
  onChanged?: () => void;
}

/** 后台解析轮询：间隔与最长等待（超时后停轮，由用户手动刷新，避免任务卡死一直转圈）。 */
const POLL_INTERVAL_MS = 2500;
const POLL_MAX_MS = 120_000;
/** 派发重新解析后的状态探测次数（后台任务启动有先后，不能假定瞬间就变为解析中）。 */
const POLL_PROBE_ATTEMPTS = 8;

function statusTag(status?: string | null) {
  const meta = structuredParseStatusMeta(status);
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

/** 记录定位文案：CSV/TSV/XLSX 给行号（含工作表），JSON 给路径。 */
function locateText(row: {
  row_no: number;
  sheet_name?: string | null;
  json_path?: string | null;
}): string {
  if (row.json_path) return row.json_path;
  return `${row.sheet_name ? `${row.sheet_name} · ` : ''}第 ${row.row_no} 行`;
}

export function StructuredPreviewDrawer({ doc, open, onClose, onChanged }: Props) {
  const [info, setInfo] = useState<StructuredParseInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [items, setItems] = useState<StructuredRecordItem[]>([]);
  const [total, setTotal] = useState(0);
  const [recordsLoading, setRecordsLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [keyword, setKeyword] = useState('');
  const [keywordDraft, setKeywordDraft] = useState('');
  const [onlyAnomaly, setOnlyAnomaly] = useState(false);
  const [refreshSeq, setRefreshSeq] = useState(0);
  const [reparsing, setReparsing] = useState(false);
  const [revectorizing, setRevectorizing] = useState(false);
  const [detail, setDetail] = useState<StructuredRecordDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const docIdRef = useRef<string | null>(null);
  const delayedTimerRef = useRef<number | null>(null);

  const clearDelayedTimer = useCallback(() => {
    if (delayedTimerRef.current !== null) {
      window.clearTimeout(delayedTimerRef.current);
      delayedTimerRef.current = null;
    }
  }, []);

  const loadInfo = useCallback(async (): Promise<StructuredParseInfo | null> => {
    if (!doc) return null;
    const id = doc.id;
    try {
      const data = await fetchStructuredParse(id);
      if (docIdRef.current !== id) return null;
      setInfo(data);
      return data;
    } catch (err) {
      if (docIdRef.current === id) setError(describeApiError(err));
      return null;
    }
  }, [doc]);

  // 处理概览（统一状态与阶段）：这个文件的「字段识别 → 数据解析 → 记录生成」走到哪一步、
  // 为什么失败、产生了多少记录，以及它是否被用在某个关联文件组里。
  const processing = useProcessingOverview<DocumentProcessingInfo>(
    () => (doc ? fetchDocumentProcessing(doc.id) : Promise.reject(new Error('未选择文件'))),
    open && !!doc,
    doc?.id,
  );

  // 打开抽屉：先取概览，再取第一页记录（记录分页由下方 effect 统一驱动）
  useEffect(() => {
    if (!open || !doc) return;
    docIdRef.current = doc.id;
    setError(null);
    setInfo(null);
    setItems([]);
    setTotal(0);
    setDetail(null);
    setKeyword('');
    setKeywordDraft('');
    setOnlyAnomaly(false);
    setPage(1);
    setPageSize(DEFAULT_PAGE_SIZE);
    setLoading(true);
    void loadInfo().finally(() => {
      if (docIdRef.current === doc.id) setLoading(false);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, doc?.id]);

  // 关闭 / 卸载：让在途请求失效，避免回写上一个文件的解析结果
  useEffect(() => {
    if (open) return;
    docIdRef.current = null;
    clearDelayedTimer();
    setInfo(null);
    setItems([]);
    setTotal(0);
    setError(null);
    setDetail(null);
    setLoading(false);
  }, [open, clearDelayedTimer]);

  useEffect(() => () => clearDelayedTimer(), [clearDelayedTimer]);

  // 分页 / 筛选变化：只请求当前这一页
  useEffect(() => {
    if (!open || !doc || docIdRef.current !== doc.id) return;
    const id = doc.id;
    const offset = (page - 1) * pageSize;
    setRecordsLoading(true);
    void fetchStructuredRecords(id, {
      limit: pageSize,
      offset,
      keyword: keyword.trim() || undefined,
      onlyAnomaly,
    })
      .then((data) => {
        if (docIdRef.current !== id) return;
        setItems(data.items);
        setTotal(data.total);
      })
      .catch((err) => {
        if (docIdRef.current === id) setError(describeApiError(err));
      })
      .finally(() => {
        if (docIdRef.current === id) setRecordsLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, doc?.id, page, pageSize, keyword, onlyAnomaly, refreshSeq]);

  // 待解析 / 解析中：轮询概览，落定后刷新当前页记录
  useEffect(() => {
    const status = info?.status;
    if (!open || !doc || !isTaskInFlight(status)) return;
    const id = doc.id;
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      if (Date.now() - startedAt > POLL_MAX_MS) {
        window.clearInterval(timer);
        return;
      }
      void loadInfo().then((next) => {
        if (!next || docIdRef.current !== id) return;
        if (!isTaskInFlight(next.status)) {
          window.clearInterval(timer);
          setRefreshSeq((value) => value + 1);
        }
      });
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [open, doc, info?.status, loadInfo]);

  /**
   * 重新解析后有限次探测状态变化。
   *
   * 后台任务稍后才把概览置为「解析中」，所以派发瞬间读到的可能仍是上一次的结果；
   * 这里最多探测 POLL_PROBE_ATTEMPTS 次，一旦状态推进就交给常规轮询接管，探测不到就停，
   * 不伪造状态也不无限轮询。
   */
  const probeUntilChanged = useCallback(
    (id: string, from: string | null, attemptsLeft: number) => {
      if (attemptsLeft <= 0) {
        setRefreshSeq((value) => value + 1);
        return;
      }
      delayedTimerRef.current = window.setTimeout(() => {
        delayedTimerRef.current = null;
        if (docIdRef.current !== id) return;
        void loadInfo().then((next) => {
          if (!next) return;
          if (isTaskInFlight(next.status)) {
            setRefreshSeq((value) => value + 1);
            return;
          }
          if (next.status !== from) {
            setRefreshSeq((value) => value + 1);
            return;
          }
          probeUntilChanged(id, from, attemptsLeft - 1);
        });
      }, POLL_INTERVAL_MS);
    },
    [loadInfo],
  );

  const handleReparse = async () => {
    if (!doc) return;
    const id = doc.id;
    setReparsing(true);
    try {
      const result = await reparseStructured(id);
      message.success(
        isTaskInFlight(result.status)
          ? '已派发重新解析，完成后自动刷新'
          : '已派发重新解析，稍后刷新结果',
      );
      onChanged?.();
      clearDelayedTimer();
      processing.reload();
      if (!isTaskInFlight(result.status)) probeUntilChanged(id, result.status, POLL_PROBE_ATTEMPTS);
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setReparsing(false);
    }
  };

  const handleRevectorize = async () => {
    if (!doc) return;
    const id = doc.id;
    setRevectorizing(true);
    try {
      const result = await revectorizeStructured(id);
      message.success(
        vectorStatusMeta(result.vector_status).label === '向量化中'
          ? '已派发重新向量化，完成后自动刷新'
          : '已派发重新向量化，稍后刷新结果',
      );
      onChanged?.();
      processing.reload();
      setRefreshSeq((value) => value + 1);
      void loadInfo();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setRevectorizing(false);
    }
  };


  const openDetail = async (record: StructuredRecordItem) => {
    if (!doc) return;
    const id = doc.id;
    setDetailLoading(true);
    try {
      const data = await fetchStructuredRecord(id, record.id);
      if (docIdRef.current !== id) return;
      setDetail(data);
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setDetailLoading(false);
    }
  };

  const fieldColumns: ColumnsType<StructuredField> = [
    {
      title: '字段',
      dataIndex: 'name',
      width: 200,
      render: (name: string) => <span style={{ fontWeight: 500 }}>{name}</span>,
    },
    {
      title: '推断类型',
      dataIndex: 'type',
      width: 110,
      render: (type: string) => <Tag>{fieldTypeLabel(type)}</Tag>,
    },
    { title: '出现次数', dataIndex: 'count', width: 90 },
    {
      title: '空值数',
      dataIndex: 'null_count',
      width: 90,
      render: (n: number) => (n > 0 ? <Tag color="orange">{n}</Tag> : '0'),
    },
    {
      title: '工作表',
      dataIndex: 'sheet',
      width: 120,
      render: (sheet?: string | null) => sheet || '-',
    },
    {
      title: '说明',
      dataIndex: 'note',
      render: (note?: string | null) => note || '-',
    },
  ];

  const recordColumns: ColumnsType<StructuredRecordItem> = [
    {
      title: '定位',
      key: 'locate',
      width: 150,
      render: (_, row) => (
        <Space size={4} wrap>
          <span>{locateText(row)}</span>
          {row.record_label && <Tag color="blue">{row.record_label}</Tag>}
        </Space>
      ),
    },
    {
      title: '独立知识记录',
      dataIndex: 'knowledge_text',
      render: (text: string) => (
        <Typography.Paragraph style={{ marginBottom: 0 }} ellipsis={{ rows: 2, tooltip: text }}>
          {text || '（空值未生成文本）'}
        </Typography.Paragraph>
      ),
    },
    {
      title: '字段 / 空值',
      key: 'counts',
      width: 110,
      render: (_, row) => `${row.field_count} / ${row.empty_field_count}`,
    },
    {
      title: '提示',
      key: 'anomalies',
      width: 200,
      render: (_, row) =>
        row.anomalies.length ? (
          <Space size={4} wrap>
            {row.anomalies.slice(0, 2).map((tip, index) => (
              <Tooltip key={index} title={tip}>
                <Tag color={tip.includes('空值') ? 'orange' : 'red'}>{tip}</Tag>
              </Tooltip>
            ))}
            {row.anomalies.length > 2 && <Tag>+{row.anomalies.length - 2}</Tag>}
          </Space>
        ) : (
          '-'
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 90,
      render: (_, row) => (
        <Button size="small" type="link" onClick={() => void openDetail(row)}>
          详情
        </Button>
      ),
    },
  ];

  const payloadColumns: ColumnsType<{ field: string; value: string; isEmpty: boolean }> = [
    { title: '原始字段', dataIndex: 'field', width: 180 },
    {
      title: '原始值',
      dataIndex: 'value',
      render: (value: string, row) =>
        row.isEmpty ? <Tag color="orange">空值</Tag> : <span>{value}</span>,
    },
  ];

  const title = (
    <Space size={8} wrap>
      <span style={{ fontWeight: 600 }}>{doc?.file_name ?? '结构化数据预览'}</span>
      {doc?.file_type && <Tag>{doc.file_type.toUpperCase()}</Tag>}
      {statusTag(info?.status)}
      {info && info.truncated && <Tag color="orange">记录数已截断</Tag>}
    </Space>
  );

  const renderSummary = () => {
    if (!info) return null;
    return (
      <Descriptions
        size="small"
        column={{ xs: 1, sm: 2, md: 3, lg: 4 }}
        items={[
          { key: 'format', label: '识别格式', children: info.detected_format || '-' },
          { key: 'encoding', label: '编码', children: info.encoding || '-' },
          { key: 'fields', label: '字段数', children: info.field_count },
          { key: 'records', label: '记录数', children: info.record_count },
          { key: 'skipped', label: '跳过行数', children: info.skipped_count },
          { key: 'anomaly', label: '含提示记录数', children: info.anomaly_count },
          {
            key: 'parsed',
            label: '解析时间',
            children: info.parsed_at ? new Date(info.parsed_at).toLocaleString('zh-CN', { hour12: false }) : '-',
          },
          {
            key: 'folder',
            label: '所在文件夹',
            children: info.folder_path || '根目录',
          },
          {
            key: 'vector_status',
            label: '向量索引',
            children: (
              <Tag color={vectorStatusMeta(info.vector_status).color} style={{ margin: 0 }}>
                {vectorStatusMeta(info.vector_status).label}
              </Tag>
            ),
          },
          { key: 'vector_count', label: '向量数量', children: info.vector_count },
          { key: 'vector_model', label: '向量模型', children: info.vector_model || '-' },
          {
            key: 'vectorized_at',
            label: '向量化时间',
            children: info.vectorized_at
              ? new Date(info.vectorized_at).toLocaleString('zh-CN', { hour12: false })
              : '-',
          },
        ]}
      />
    );
  };

  const renderNotice = () => {
    if (!info) return null;
    if (!info.supported) {
      return (
        <Alert
          type="info"
          showIcon
          message={`该格式（${doc?.file_type ?? ''}）不在结构化解析范围内`}
          description="结构化解析支持 CSV / TSV / XLSX / JSON；这类文件仍可预览、下载、替换与移动，不会生成知识记录。"
        />
      );
    }
    if (info.status === 'failed') {
      return (
        <Alert
          type="error"
          showIcon
          message="解析失败，原有解析结果已保留"
          description={
            <Space direction="vertical" size={4}>
              <span>{info.error_message || '未返回失败原因'}</span>
              <span style={{ color: '#8c8c8c' }}>
                修正文件后重新上传替换，或点击右上「重新解析」；失败不会清空该文件已有的 {info.record_count} 条记录。
              </span>
            </Space>
          }
        />
      );
    }
    if (isTaskInFlight(info.status)) {
      return (
        <Alert
          type="info"
          showIcon
          message="正在识别字段并生成记录，完成后自动刷新"
          description="解析只在后台写数据库，完成后会自动把有效记录写入统一向量索引。"
        />
      );
    }
    if (info.status === 'success' && info.vector_status === 'failed') {
      return (
        <Alert
          type="error"
          showIcon
          message="向量化失败，现有向量索引保持不变"
          description={
            <Space direction="vertical" size={4}>
              <span>{info.vector_error || '未返回失败原因'}</span>
              <span style={{ color: '#8c8c8c' }}>
                可以点击右上「重新向量化」重试；失败不会清空此前已生效的向量。
              </span>
            </Space>
          }
        />
      );
    }
    if (info.status === 'success' && info.vector_status === 'indexing') {
      return (
        <Alert
          type="info"
          showIcon
          message="正在把知识记录写入统一向量索引"
          description="完成后这些记录即可在智能问答中被检索，期间旧向量仍然可用。"
        />
      );
    }
    if (info.truncated) {
      return (
        <Alert
          type="warning"
          showIcon
          message="记录数达到上限，后续内容未解析"
          description={`已生成 ${info.record_count} 条记录（单次解析上限 20000 条），建议按目录拆分为多个文件后分别上传。`}
        />
      );
    }
    return null;
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      width="90vw"
      destroyOnHidden
      title={title}
      styles={{ body: { padding: 16, overflow: 'auto' } }}
      extra={
        <Space wrap>
          <Button
            icon={<ReloadOutlined />}
            loading={loading}
            onClick={() => {
              void loadInfo();
              setRefreshSeq((value) => value + 1);
            }}
          >
            刷新
          </Button>
          {info?.supported !== false && (
            <Button type="primary" loading={reparsing} onClick={() => void handleReparse()}>
              重新解析
            </Button>
          )}
          {info?.status === 'success' && info.record_count > 0 && (
            <Tooltip
              title={
                info.vector_status === 'indexing'
                  ? '正在向量化中，完成后可再次触发'
                  : '用已生成的记录重建统一向量索引（不会重复写入）'
              }
            >
              <Button
                icon={<CloudSyncOutlined />}
                loading={revectorizing}
                disabled={info.vector_status === 'indexing'}
                onClick={() => void handleRevectorize()}
              >
                重新向量化
              </Button>
            </Tooltip>
          )}
        </Space>
      }
    >
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 60 }}>
          <Spin size="large" />
        </div>
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {error && <Alert type="error" showIcon message={error} />}
          <ProcessingOverviewPanel
            view={processing.view}
            error={processing.error}
            extra={
              processing.view?.duplicate_names?.length || processing.view?.groups?.length ? (
                <Space direction="vertical" size={2}>
                  {processing.view?.duplicate_names?.length ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {`同库内还有内容完全相同的文件：${processing.view.duplicate_names
                        .map((n) => `「${n}」`)
                        .join('、')}（仅提示，不会自动删除或覆盖）`}
                    </Typography.Text>
                  ) : null}
                  {processing.view?.groups?.length ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {`该文件还被这些关联数据文件组使用：${processing.view.groups
                        .map((g) => `「${g.name || g.id}」（${g.record_count} 条关联记录）`)
                        .join('、')}`}
                    </Typography.Text>
                  ) : null}
                </Space>
              ) : null
            }
          />
          {renderNotice()}
          {info && (
            <div>{renderSummary()}</div>
          )}
          {info && info.fields.length > 0 && (
            <div>
              <Typography.Title level={5} style={{ marginTop: 0 }}>
                识别到的字段
              </Typography.Title>
              <Table<StructuredField>
                size="small"
                rowKey={(row) => `${row.sheet ?? ''}-${row.name}`}
                columns={fieldColumns}
                dataSource={info.fields}
                pagination={false}
                scroll={{ x: 'max-content', y: 240 }}
              />
            </div>
          )}
          <div>
            <Space wrap style={{ marginBottom: 8 }}>
              <Typography.Title level={5} style={{ margin: 0 }}>
                知识记录（共 {total} 条）
              </Typography.Title>
              <Input
                allowClear
                size="small"
                prefix={<SearchOutlined />}
                placeholder="按记录文本搜索"
                style={{ width: 220 }}
                value={keywordDraft}
                onChange={(event) => {
                  const value = event.target.value;
                  setKeywordDraft(value);
                  // 清空输入框时立即恢复完整列表，避免「已清除关键词却仍在过滤」
                  if (!value) {
                    setKeyword('');
                    setPage(1);
                  }
                }}
                onPressEnter={() => {
                  setKeyword(keywordDraft);
                  setPage(1);
                }}
              />
              <Checkbox
                checked={onlyAnomaly}
                onChange={(event) => {
                  setOnlyAnomaly(event.target.checked);
                  setPage(1);
                }}
              >
                只看空值/异常
              </Checkbox>
              <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                每页 {pageSize} 条，翻页时才请求下一页
              </span>
            </Space>
            <Table<StructuredRecordItem>
              size="small"
              rowKey="id"
              loading={recordsLoading}
              columns={recordColumns}
              dataSource={items}
              scroll={{ x: 'max-content' }}
              locale={{
                emptyText:
                  info?.status === 'success' ? (
                    <Empty description="没有符合当前筛选的记录" />
                  ) : (
                    <Empty description="该文件还没有生成知识记录" />
                  ),
              }}
              pagination={{
                current: page,
                pageSize,
                total,
                showSizeChanger: true,
                pageSizeOptions: PAGE_SIZE_OPTIONS,
                showTotal: (count) => `共 ${count} 条`,
                onChange: (nextPage, nextSize) => {
                  setPage(nextSize !== pageSize ? 1 : nextPage);
                  setPageSize(nextSize);
                },
              }}
            />
          </div>
        </Space>
      )}

      <Modal
        open={!!detail || detailLoading}
        title="记录详情与来源追溯"
        width={720}
        onCancel={() => setDetail(null)}
        onOk={() => setDetail(null)}
        okText="关闭"
        cancelButtonProps={{ style: { display: 'none' } }}
        destroyOnHidden
      >
        {detailLoading || !detail ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: 30 }}>
            <Spin />
          </div>
        ) : (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions
              size="small"
              column={1}
              items={[
                { key: 'file', label: '原始文件', children: `${detail.file_name}（${detail.doc_id}）` },
                { key: 'folder', label: '所在文件夹', children: detail.folder_path || '根目录' },
                {
                  key: 'locate',
                  label: '原始位置',
                  children: locateText(detail),
                },
                {
                  key: 'text',
                  label: '生成的知识记录',
                  children: detail.knowledge_text || '（空值未生成文本）',
                },
                {
                  key: 'anomaly',
                  label: '空值与异常提示',
                  children: detail.anomalies.length ? (
                    <Space size={4} wrap>
                      {detail.anomalies.map((tip, index) => (
                        <Tag key={index} color={tip.includes('空值') ? 'orange' : 'red'}>
                          {tip}
                        </Tag>
                      ))}
                    </Space>
                  ) : (
                    '无'
                  ),
                },
              ]}
            />
            <Table
              size="small"
              rowKey="field"
              columns={payloadColumns}
              dataSource={Object.keys(detail.payload).map((field) => {
                const value = cellValueText(detail.payload[field]);
                return { field, value, isEmpty: value.trim() === '' };
              })}
              pagination={false}
              scroll={{ y: 280 }}
            />
          </Space>
        )}
      </Modal>
    </Drawer>
  );
}
