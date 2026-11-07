'use client';

import { createElement, useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tree,
  Typography,
  message,
} from 'antd';
import {
  DownloadOutlined,
  EditOutlined,
  SaveOutlined,
  UndoOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { DocumentItem, DocumentPreview, DocumentProcessingInfo } from '@/types';
import {
  fetchDocumentContent,
  fetchDocumentPreview,
  fetchDocumentProcessing,
  saveDocumentContent,
} from '@/services/api';
import { useProcessingOverview } from '@/hooks/useProcessingOverview';
import { ProcessingOverviewPanel } from '@/components/common/ProcessingStatusTag';
import { describeApiError } from '@/utils/apiError';
import { downloadBlob } from '@/utils/download';
import {
  normalizeTableRows,
  serializeDelimited,
  type TableDelimiter,
} from '@/utils/delimited';

const { Text } = Typography;

interface Props {
  doc: DocumentItem | null;
  open: boolean;
  onClose: () => void;
  onSaved?: () => void;
  /** 打开即进入编辑态（列表「编辑」按钮使用）。 */
  initialEditing?: boolean;
}

interface PreviewRow {
  key: number;
  cells: string[];
}

/**
 * 渲染清洗后 HTML 的沙箱文档。
 *
 * 服务端已按白名单重建标签与属性，前端再用 sandbox（空值=禁脚本/禁表单/禁同源）
 * 与 CSP 双层兜底，确保上传文件中的脚本不会执行。
 */
function buildSandboxDoc(html: string): string {
  return [
    '<!doctype html><html><head><meta charset="utf-8">',
    `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; `,
    "img-src data: https:; style-src 'unsafe-inline'; font-src data:\">",
    '<style>body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;',
    'margin:16px;line-height:1.7;color:#333;word-break:break-word}',
    'table{border-collapse:collapse}th,td{border:1px solid #d9d9d9;padding:4px 8px}',
    'img{max-width:100%}</style></head><body>',
    html,
    '</body></html>',
  ].join('');
}

export function DocumentPreviewDrawer({
  doc,
  open,
  onClose,
  onSaved,
  initialEditing = false,
}: Props) {
  const [preview, setPreview] = useState<DocumentPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [textDraft, setTextDraft] = useState('');
  const [tableDraft, setTableDraft] = useState<string[][]>([]);
  const [saving, setSaving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const blobUrlRef = useRef<string | null>(null);
  const requestSeqRef = useRef(0);

  const replaceBlobUrl = useCallback((url: string | null) => {
    if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current);
    blobUrlRef.current = url;
    setBlobUrl(url);
  }, []);

  const load = useCallback(async () => {
    if (!doc) return;
    const seq = ++requestSeqRef.current;
    setLoading(true);
    setError(null);
    setEditing(false);
    try {
      const data = await fetchDocumentPreview(doc.id);
      let nextBlobUrl: string | null = null;
      if (data.kind === 'pdf' || data.kind === 'image') {
        const blob = await fetchDocumentContent(doc.id);
        nextBlobUrl = URL.createObjectURL(blob);
      }
      if (seq !== requestSeqRef.current) {
        if (nextBlobUrl) URL.revokeObjectURL(nextBlobUrl);
        return;
      }
      setPreview(data);
      setTextDraft(data.content ?? '');
      setTableDraft(normalizeTableRows(data.rows ?? []));
      setEditing(initialEditing && data.editable && !data.truncated);
      replaceBlobUrl(nextBlobUrl);
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      setPreview(null);
      setError(describeApiError(err));
      replaceBlobUrl(null);
    } finally {
      if (seq === requestSeqRef.current) setLoading(false);
    }
  }, [doc, initialEditing, replaceBlobUrl]);

  useEffect(() => {
    if (open && doc) void load();
    // 仅按文档 id 触发，避免父级轮询生成新对象时反复重载
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, doc?.id]);

  // 处理概览（第五阶段）：这个文件处理过没有、走到哪一步、为什么失败、产生了什么结果。
  // 只读派生接口，pending 也刷新一次是因为上传/保存后任务刚派发、状态还没落到 parsing。
  const processing = useProcessingOverview<DocumentProcessingInfo>(
    () => (doc ? fetchDocumentProcessing(doc.id) : Promise.reject(new Error('未选择文件'))),
    open && !!doc,
    doc?.id,
    (view) => view.status === 'processing' || view.status === 'pending',
  );

  useEffect(() => {
    if (!open) {
      // 使仍在途的请求失效，避免关闭后回写旧预览
      requestSeqRef.current += 1;
      replaceBlobUrl(null);
      setPreview(null);
      setError(null);
      setEditing(false);
      setLoading(false);
    }
  }, [open, replaceBlobUrl]);

  useEffect(
    () => () => {
      requestSeqRef.current += 1;
      replaceBlobUrl(null);
    },
    [replaceBlobUrl],
  );

  const handleDownload = async () => {
    if (!doc) return;
    setDownloading(true);
    try {
      const blob = await fetchDocumentContent(doc.id);
      downloadBlob(blob, doc.file_name);
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setDownloading(false);
    }
  };

  const startEdit = () => {
    setTextDraft(preview?.content ?? '');
    setTableDraft(normalizeTableRows(preview?.rows ?? []));
    setEditing(true);
  };

  const handleSave = async () => {
    if (!doc || !preview) return;
    const delimiter: TableDelimiter = preview.delimiter === '\t' ? '\t' : ',';
    const content =
      preview.kind === 'table'
        ? serializeDelimited(normalizeTableRows(tableDraft), delimiter)
        : textDraft;
    setSaving(true);
    try {
      const result = await saveDocumentContent(
        doc.id,
        content,
        preview.encoding ?? undefined,
      );
      message.success(result.message || '保存成功');
      setEditing(false);
      await load();
      // 保存会派发重新解析/向量化：概览立刻重读，进度才跟得上真实状态
      processing.reload();
      onSaved?.();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setSaving(false);
    }
  };

  const updateCell = (rowIndex: number, colIndex: number, value: string) => {
    setTableDraft((prev) => {
      const next = prev.map((row) => [...row]);
      if (!next[rowIndex]) next[rowIndex] = [];
      while (next[rowIndex].length <= colIndex) next[rowIndex].push('');
      next[rowIndex][colIndex] = value;
      return next;
    });
  };

  const addRow = () => {
    setTableDraft((prev) => {
      const width = Math.max(prev[0]?.length ?? 1, 1);
      return [...prev, Array.from({ length: width }, () => '')];
    });
  };

  const buildColumns = (rows: string[][], editable: boolean): ColumnsType<PreviewRow> => {
    const width = rows.reduce((max, row) => Math.max(max, row.length), 0);
    return Array.from({ length: width }, (_, colIndex) => ({
      key: `col-${colIndex}`,
      dataIndex: ['cells', colIndex],
      ellipsis: !editable,
      title: editable ? (
        <Input
          size="small"
          value={rows[0]?.[colIndex] ?? ''}
          placeholder={`列${colIndex + 1}`}
          onChange={(event) => updateCell(0, colIndex, event.target.value)}
        />
      ) : (
        rows[0]?.[colIndex] || `列${colIndex + 1}`
      ),
      render: (value: string, record: PreviewRow) =>
        editable ? (
          <Input
            size="small"
            value={record.cells[colIndex] ?? ''}
            onChange={(event) => updateCell(record.key, colIndex, event.target.value)}
          />
        ) : (
          value ?? ''
        ),
    }));
  };

  const renderTable = (rows: string[][], editable: boolean) => {
    if (!rows.length) {
      return <Empty description="表格为空" />;
    }
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minHeight: 0 }}>
        {editable && (
          <Space>
            <Button size="small" onClick={addRow}>
              添加行
            </Button>
            <span style={{ color: '#8c8c8c', fontSize: 12 }}>
              {doc?.space === 'structured'
                ? '可直接修改单元格，保存后只更新文件本身（不切片、不向量化；CSV / TSV / JSON 会按新内容重新识别字段并生成记录）'
                : '可直接修改单元格，保存后重新解析并更新检索索引'}
            </span>
          </Space>
        )}
        <Table<PreviewRow>
          size="small"
          bordered
          columns={buildColumns(rows, editable)}
          dataSource={rows.slice(1).map((cells, index) => ({
            key: index + 1,
            cells,
          }))}
          pagination={false}
          scroll={{ x: 'max-content', y: editable ? 420 : undefined }}
        />
      </div>
    );
  };

  const renderSourceEditor = () => (
    <Input.TextArea
      value={textDraft}
      onChange={(event) => setTextDraft(event.target.value)}
      style={{ flex: 1, minHeight: 0, resize: 'none', fontFamily: 'monospace' }}
    />
  );

  const sourceBoxStyle: CSSProperties = {
    flex: 1,
    minHeight: 0,
    margin: 0,
    padding: 12,
    overflow: 'auto',
    background: '#fafafa',
    border: '1px solid #f0f0f0',
    borderRadius: 6,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontFamily: 'monospace',
  };

  const renderSourceView = (text?: string | null) => (
    <pre style={sourceBoxStyle}>{text || '（空文件）'}</pre>
  );

  const renderPreview = () => {
    if (!preview) return <Empty description="暂无预览内容" />;

    switch (preview.kind) {
      case 'text':
        return editing ? (
          renderSourceEditor()
        ) : (
          renderSourceView(preview.content)
        );
      case 'json':
        return editing ? (
          renderSourceEditor()
        ) : (
          <Tabs
            style={{ flex: 1, minHeight: 0 }}
            items={[
              ...(preview.tree?.length
                ? [
                    {
                      key: 'tree',
                      label: '结构',
                      children: (
                        <div
                          style={{
                            flex: 1,
                            minHeight: 0,
                            maxHeight: 'calc(100vh - 260px)',
                            overflow: 'auto',
                            padding: 8,
                            background: '#fafafa',
                            border: '1px solid #f0f0f0',
                            borderRadius: 6,
                          }}
                        >
                          <Tree treeData={preview.tree} defaultExpandAll showLine />
                        </div>
                      ),
                    },
                  ]
                : []),
              {
                key: 'source',
                label: '源码',
                children: (
                  <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 260px)' }}>
                    {renderSourceView(preview.content)}
                  </div>
                ),
              },
            ]}
          />
        );
      case 'html':
        return editing ? (
          renderSourceEditor()
        ) : (
          <Tabs
            style={{ flex: 1, minHeight: 0 }}
            items={[
              {
                key: 'render',
                label: '渲染',
                children: (
                  <iframe
                    title={`${preview.file_name} 渲染预览`}
                    sandbox=""
                    srcDoc={buildSandboxDoc(preview.html ?? '')}
                    style={{
                      width: '100%',
                      height: 'calc(100vh - 260px)',
                      border: '1px solid #f0f0f0',
                      borderRadius: 6,
                      background: '#fff',
                    }}
                  />
                ),
              },
              {
                key: 'source',
                label: '源码',
                children: (
                  <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 260px)' }}>
                    {renderSourceView(preview.content)}
                  </div>
                ),
              },
            ]}
          />
        );
      case 'table':
        return renderTable(editing ? tableDraft : preview.rows ?? [], editing);
      case 'docx':
        return (
          <div style={{ flex: 1, minHeight: 0, overflow: 'auto', paddingRight: 8 }}>
            {preview.blocks?.length ? (
              preview.blocks.map((block, index) => {
                if (block.type === 'heading') {
                  const level = Math.min(Math.max(block.level ?? 2, 1), 6);
                  return createElement(
                    `h${level}`,
                    { key: index, style: { margin: '16px 0 8px' } },
                    block.text,
                  );
                }
                return (
                  <p
                    key={index}
                    style={{ margin: '0 0 8px', whiteSpace: 'pre-wrap', lineHeight: 1.8 }}
                  >
                    {block.text}
                  </p>
                );
              })
            ) : (
              <Empty description="文档中未提取到文本内容" />
            )}
          </div>
        );
      case 'xlsx': {
        const sheets = preview.sheets ?? [];
        if (!sheets.length) return <Empty description="工作簿为空" />;
        return (
          <Tabs
            style={{ flex: 1, minHeight: 0 }}
            items={sheets.map((sheet, index) => ({
              key: `${index}-${sheet.name}`,
              label: sheet.name || `工作表${index + 1}`,
              children: (
                <div style={{ maxHeight: 'calc(100vh - 240px)', overflow: 'auto' }}>
                  {renderTable(sheet.rows, false)}
                </div>
              ),
            }))}
          />
        );
      }
      case 'pptx':
        return (
          <div style={{ flex: 1, minHeight: 0, overflow: 'auto', paddingRight: 8 }}>
            {preview.slides?.length ? (
              preview.slides.map((slide) => (
                <section key={slide.index} style={{ marginBottom: 18 }}>
                  <Tag color="blue">第 {slide.index} 页</Tag>
                  {slide.lines.length ? (
                    slide.lines.map((line, index) => (
                      <p
                        key={index}
                        style={{ margin: '4px 0', whiteSpace: 'pre-wrap', lineHeight: 1.7 }}
                      >
                        {line}
                      </p>
                    ))
                  ) : (
                    <span style={{ color: '#8c8c8c' }}>（本页无文本）</span>
                  )}
                </section>
              ))
            ) : (
              <Empty description="演示文稿中未提取到文本" />
            )}
          </div>
        );
      case 'pdf':
        return blobUrl ? (
          <iframe
            title={preview.file_name}
            src={blobUrl}
            style={{ flex: 1, minHeight: 0, width: '100%', border: 0 }}
          />
        ) : (
          <Spin style={{ margin: 'auto' }} />
        );
      case 'image':
        return blobUrl ? (
          <div
            style={{
              flex: 1,
              minHeight: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              overflow: 'auto',
            }}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={blobUrl}
              alt={preview.file_name}
              style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }}
            />
          </div>
        ) : (
          <Spin style={{ margin: 'auto' }} />
        );
      default:
        return (
          <Empty description="当前格式暂不支持站内预览，请点击右上角「下载」后查看" />
        );
    }
  };

  const title = (
    <Space size={8} wrap>
      <span style={{ fontWeight: 600 }}>{doc?.file_name ?? '文档预览'}</span>
      {preview && <Tag>{preview.file_type.toUpperCase()}</Tag>}
      {preview?.encoding && <Tag color="blue">{preview.encoding.toUpperCase()}</Tag>}
      {preview?.truncated && <Tag color="orange">已截断</Tag>}
    </Space>
  );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      width="90vw"
      destroyOnHidden
      title={title}
      styles={{
        body: {
          padding: 16,
          display: 'flex',
          flexDirection: 'column',
          height: 'calc(100vh - 57px)',
          overflow: 'hidden',
        },
      }}
      extra={
        <Space wrap>
          <Button
            icon={<DownloadOutlined />}
            loading={downloading}
            onClick={() => void handleDownload()}
          >
            下载
          </Button>
          {preview?.editable && !editing && (
            <Button icon={<EditOutlined />} onClick={startEdit}>
              编辑
            </Button>
          )}
          {editing && (
            <>
              <Button icon={<UndoOutlined />} disabled={saving} onClick={() => setEditing(false)}>
                取消
              </Button>
              <Button
                type="primary"
                icon={<SaveOutlined />}
                loading={saving}
                onClick={() => void handleSave()}
              >
                保存
              </Button>
            </>
          )}
        </Space>
      }
    >
      {loading ? (
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Space direction="vertical" align="center">
            <Spin size="large" />
            <span style={{ color: '#8c8c8c' }}>加载中...</span>
          </Space>
        </div>
      ) : error ? (
        <Alert type="error" showIcon message="加载文档失败" description={error} />
      ) : (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
          }}
        >
          <ProcessingOverviewPanel
            view={processing.view}
            error={processing.error}
            extra={
              processing.view?.duplicate_names?.length ? (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {`同库内还有内容完全相同的文件：${processing.view.duplicate_names
                    .map((n) => `「${n}」`)
                    .join('、')}（仅提示，不会自动删除或覆盖）`}
                </Text>
              ) : null
            }
          />
          {preview?.message && (
            <Alert
              type={preview.kind === 'unsupported' ? 'warning' : 'info'}
              showIcon
              message={preview.message}
            />
          )}
          {renderPreview()}
        </div>
      )}
    </Drawer>
  );
}
