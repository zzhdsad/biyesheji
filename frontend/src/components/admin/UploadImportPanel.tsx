'use client';

/**
 * 数据导入中心 · 上传文件导入面板。
 *
 * 流程（严格"先预览后写入"，绝不猜测后直接导入）：
 *   选择文件 → 后端自动识别类型/字段 → 展示映射与预估 → 用户确认 → 真正导入
 *
 * 约束与后端一致（src/application/upload_import_service.py）：
 * - 无法可靠识别类型时显示"无法自动识别，请手动选择"，不提供导入入口；
 * - 已存在同名资源只补充空字段，不覆盖已维护数据；
 * - 默认不触发大批量向量化（需显式勾选目标知识库）；
 * - 导入结果展示 新增/更新/跳过/失败 与失败原因。
 */

import { useMemo, useState } from 'react';
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  Upload,
} from 'antd';
import { InboxOutlined } from '@ant-design/icons';

import {
  commitImportUpload,
  fetchKnowledgeBases,
  previewImportUpload,
  uploadImportFile,
  type ImportCommitResult,
  type ImportTargetType,
  type ImportUploadPreview,
} from '@/services/api';
import type { KnowledgeBase } from '@/types';

const TYPE_LABEL: Record<string, string> = {
  herb: '中药',
  prescription: '方剂',
  theory: '中医理论',
  literature: '文献',
  unknown: '无法自动识别',
};

const TYPE_OPTIONS = [
  { label: '中药', value: 'herb' },
  { label: '方剂', value: 'prescription' },
  { label: '中医理论', value: 'theory' },
  { label: '文献', value: 'literature' },
];

const FIELD_LABEL: Record<string, string> = {
  name: '名称（必填）',
  aliases: '别名',
  properties: '性味/药性',
  channels: '归经',
  effects: '功效',
  efficacy: '功效',
  indications: '主治',
  usage_method: '用法用量',
  author: '作者',
  dynasty: '朝代',
  summary: '摘要',
  source: '来源',
  description: '描述',
  content: '正文/释义',
};

const ACCEPT = '.csv,.tsv,.xlsx,.json,.jsonl,.ndjson,.txt,.md,.docx,.pdf';

export default function UploadImportPanel() {
  const { message } = App.useApp();
  const [preview, setPreview] = useState<ImportUploadPreview | null>(null);
  const [uploading, setUploading] = useState(false);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [targetType, setTargetType] = useState<ImportTargetType | undefined>(undefined);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbId, setKbId] = useState<string | undefined>(undefined);
  const [vectorize, setVectorize] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [result, setResult] = useState<ImportCommitResult | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const loadKbsOnce = async () => {
    if (kbs.length === 0) {
      try {
        setKbs(await fetchKnowledgeBases());
      } catch {
        /* 知识库列表失败不影响导入 */
      }
    }
  };

  const onUpload = async (file: File) => {
    setUploading(true);
    setResult(null);
    try {
      const res = await uploadImportFile(file);
      setPreview(res);
      setTargetType(res.target_type);
      setMapping(
        Object.fromEntries(
          Object.entries(res.field_mapping ?? {}).filter(([, v]) => Boolean(v)) as [
            string,
            string,
          ][],
        ),
      );
      void loadKbsOnce();
      if (res.need_user_type) {
        message.warning('无法自动识别数据类型，请手动选择后再预览');
      } else {
        message.success(`已识别为「${TYPE_LABEL[res.target_type]}」，共 ${res.total_rows} 条`);
      }
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail;
      message.error(detail ?? (err as Error).message ?? '文件解析失败');
    } finally {
      setUploading(false);
    }
    return false; // 阻止 antd 默认上传行为
  };

  const onRetype = async (value: ImportTargetType) => {
    if (!preview) return;
    setTargetType(value);
    try {
      const res = await previewImportUpload(preview.dataset_id, value);
      setPreview(res);
      setMapping(
        Object.fromEntries(
          Object.entries(res.field_mapping ?? {}).filter(([, v]) => Boolean(v)) as [
            string,
            string,
          ][],
        ),
      );
    } catch (err) {
      message.error((err as Error).message ?? '重新预览失败');
    }
  };

  const effectiveType = preview?.target_type ?? 'unknown';
  const canImport =
    Boolean(preview) && effectiveType !== 'unknown' && Object.keys(mapping).includes('name');

  const mappingRows = useMemo(
    () =>
      Object.entries(preview?.field_mapping ?? {}).map(([field, suggested]) => ({
        field,
        suggested: suggested ?? '',
      })),
    [preview],
  );

  const onCommit = async () => {
    if (!preview || !targetType) return;
    setCommitting(true);
    try {
      const res = await commitImportUpload({
        dataset_id: preview.dataset_id,
        target_type: targetType,
        field_mapping: mapping,
        kb_id: vectorize ? (kbId ?? null) : null,
        vectorize: Boolean(vectorize && kbId),
        confirmed: true,
      });
      setResult(res);
      setConfirmOpen(false);
      message.success(
        `导入完成：新增 ${res.inserted}，更新 ${res.updated}，跳过 ${res.skipped}，失败 ${res.failed}`,
      );
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail;
      message.error(detail ?? (err as Error).message ?? '导入失败');
    } finally {
      setCommitting(false);
    }
  };

  return (
    <Card title="上传文件导入（CSV / XLSX / JSON / TXT / MD / DOCX / PDF）" style={{ marginBottom: 16 }}>
      <Upload.Dragger
        accept={ACCEPT}
        multiple={false}
        showUploadList={false}
        beforeUpload={(file) => void onUpload(file as File)}
      >
        <p className="ant-upload-drag-icon">
          <InboxOutlined />
        </p>
        <p className="ant-upload-text">点击或拖拽文件到此处上传</p>
        <p className="ant-upload-hint">
          支持 {ACCEPT}。上传后仅做解析与预览，<b>不会直接写入数据库</b>。
        </p>
      </Upload.Dragger>

      {uploading && <p style={{ marginTop: 12 }}>解析中…</p>}

      {preview && (
        <div style={{ marginTop: 16 }}>
          <Descriptions size="small" bordered column={2} style={{ marginBottom: 12 }}>
            <Descriptions.Item label="文件">{preview.file_name}</Descriptions.Item>
            <Descriptions.Item label="类型">{preview.file_type || '—'}</Descriptions.Item>
            <Descriptions.Item label="解析行数">{preview.total_rows}</Descriptions.Item>
            <Descriptions.Item label="自动识别结果">
              {preview.detected_type === 'unknown' ? (
                <Tag color="warning">无法自动识别</Tag>
              ) : (
                <Tag color="blue">{TYPE_LABEL[preview.detected_type]}</Tag>
              )}
              {preview.detected_scores && (
                <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
                  {Object.entries(preview.detected_scores)
                    .map(([k, v]) => `${TYPE_LABEL[k] ?? k}:${v}`)
                    .join('  ')}
                </Typography.Text>
              )}
            </Descriptions.Item>
          </Descriptions>

          <Space size="middle" style={{ marginBottom: 12 }} wrap>
            <span>
              目标类型：
              <Select
                style={{ width: 160 }}
                value={targetType}
                options={TYPE_OPTIONS}
                onChange={(v) => void onRetype(v)}
                placeholder="请选择"
              />
            </span>
            <span>
              导入后向量化到：
              <Select
                style={{ width: 220 }}
                allowClear
                placeholder="默认不自动向量化"
                value={kbId}
                options={kbs.map((k) => ({ label: k.name, value: k.id }))}
                onChange={setKbId}
              />
            </span>
            <Checkbox
              checked={vectorize}
              onChange={(e) => setVectorize(e.target.checked)}
              disabled={!kbId}
            >
              导入后进入向量流程
            </Checkbox>
          </Space>

          {effectiveType === 'unknown' && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="无法自动识别数据类型"
              description="请在上方手动选择数据类型（中药 / 方剂 / 中医理论 / 文献），确认字段映射后再导入。"
            />
          )}

          <Row gutter={16} style={{ marginBottom: 12 }}>
            <Col span={6}>
              <Statistic title="预计新增" value={preview.estimate.insert} />
            </Col>
            <Col span={6}>
              <Statistic title="预计更新（仅补空字段）" value={preview.estimate.update} />
            </Col>
            <Col span={6}>
              <Statistic title="预计跳过" value={preview.estimate.skip} />
            </Col>
            <Col span={6}>
              <Statistic title="预计失败" value={preview.estimate.invalid} />
            </Col>
          </Row>

          <Typography.Title level={5} style={{ marginTop: 8 }}>
            字段映射（目标字段 ← 文件中的列）
          </Typography.Title>
          <Table
            size="small"
            rowKey="field"
            pagination={false}
            dataSource={mappingRows}
            locale={{ emptyText: '请先选择数据类型' }}
            columns={[
              {
                title: '目标字段',
                dataIndex: 'field',
                width: 180,
                render: (v: string) => FIELD_LABEL[v] ?? v,
              },
              {
                title: '对应列',
                dataIndex: 'suggested',
                render: (_v, row) => (
                  <Select
                    style={{ width: 260 }}
                    allowClear
                    placeholder="未映射（保持为空）"
                    value={mapping[row.field] ?? (row.suggested || undefined)}
                    options={preview.headers.map((h) => ({ label: h, value: h }))}
                    onChange={(value) =>
                      setMapping((cur) => {
                        const next = { ...cur };
                        if (value) next[row.field] = value;
                        else delete next[row.field];
                        return next;
                      })
                    }
                  />
                ),
              },
            ]}
          />

          <Space style={{ marginTop: 16 }}>
            <Button
              type="primary"
              disabled={!canImport}
              loading={committing}
              onClick={() => setConfirmOpen(true)}
            >
              确认导入
            </Button>
            {!canImport && (
              <Typography.Text type="secondary">
                需要选择数据类型并映射「名称」字段后才能导入
              </Typography.Text>
            )}
          </Space>
        </div>
      )}

      {result && (
        <div style={{ marginTop: 16 }}>
          <Descriptions size="small" bordered column={4}>
            <Descriptions.Item label="任务状态">{result.status}</Descriptions.Item>
            <Descriptions.Item label="总数">{result.total}</Descriptions.Item>
            <Descriptions.Item label="新增">{result.inserted}</Descriptions.Item>
            <Descriptions.Item label="更新">{result.updated}</Descriptions.Item>
            <Descriptions.Item label="跳过">{result.skipped}</Descriptions.Item>
            <Descriptions.Item label="失败">{result.failed}</Descriptions.Item>
            <Descriptions.Item label="批次号">{result.batch_id}</Descriptions.Item>
            <Descriptions.Item label="任务 ID">{result.job_id}</Descriptions.Item>
          </Descriptions>
          {result.failed_items.length > 0 && (
            <Table
              size="small"
              style={{ marginTop: 8 }}
              rowKey={(_r, i) => String(i)}
              pagination={{ pageSize: 5 }}
              dataSource={result.failed_items}
              columns={[
                { title: '行', dataIndex: 'row', width: 80 },
                { title: '名称', dataIndex: 'name', width: 160 },
                { title: '原因', dataIndex: 'reason' },
              ]}
            />
          )}
          {result.vectorize_result && (
            <Alert
              style={{ marginTop: 8 }}
              type="info"
              showIcon
              message={`向量化：请求 ${result.vectorize_result.requested} 条，完成 ${result.vectorize_result.vectorized} 条`}
            />
          )}
        </div>
      )}

      <Modal
        title="确认导入"
        open={confirmOpen}
        okText="确认导入"
        cancelText="取消"
        confirmLoading={committing}
        onCancel={() => setConfirmOpen(false)}
        onOk={() => void onCommit()}
      >
        <p>
          即将把 <b>{preview?.file_name}</b> 中的 <b>{preview?.total_rows}</b> 条记录导入到
          <b>{TYPE_LABEL[effectiveType]}</b>。
        </p>
        <ul>
          <li>预计新增 {preview?.estimate.insert} 条，更新 {preview?.estimate.update} 条（仅补充空字段）</li>
          <li>已存在的同名资源不会被覆盖，只填充当前为空的字段</li>
          <li>单条失败会记录原因并继续处理其余记录</li>
          <li>{vectorize && kbId ? '导入后将按所选知识库进入向量流程' : '本次不会自动触发向量化'}</li>
        </ul>
      </Modal>
    </Card>
  );
}
