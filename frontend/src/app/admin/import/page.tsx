'use client';

import { useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Drawer,
  Empty,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  CloudUploadOutlined,
  DatabaseOutlined,
  ImportOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import UploadImportPanel from '@/components/admin/UploadImportPanel';
import {
  cancelImportJob,
  createImportJob,
  executeCleanup,
  fetchCleanupStats,
  fetchImportConfig,
  fetchKnowledgeBases,
  fetchLatestImportJob,
  fetchLatestResourceVectorizeJob,
  fetchResourceVectorizeStats,
  createResourceVectorizeJob,
  resumeResourceVectorizeJob,
  planCleanup,
  scanImportDatasets,
  type ImportDataset,
  type ImportFieldMapping,
  type ImportJobStatus,
} from '@/services/api';

const TYPE_LABEL: Record<string, string> = {
  herb: '中药',
  prescription: '方剂',
  theory: '中医理论',
  literature: '文献',
  document: '文档（RAG）',
  relation: '关联表（不可直接导入）',
  unknown: '未识别（需人工确认）',
};

const STATUS_COLOR: Record<string, string> = {
  mapped: 'green',
  pending: 'orange',
  ignored: 'default',
};

const JOB_STATUS_COLOR: Record<string, string> = {
  pending: 'default',
  processing: 'processing',
  completed: 'success',
  failed: 'error',
  cancelled: 'warning',
};

const JOB_STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  processing: '导入中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

function formatSize(bytes: number): string {
  if (!bytes) return '—';
  const mb = bytes / 1024 / 1024;
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(2)} MB`;
}

function formatCount(ds: ImportDataset): string {
  if (ds.record_count === null) return '未知';
  const n = ds.record_count.toLocaleString('zh-CN');
  if (ds.record_count_method === 'estimated') return `≈ ${n}（估算）`;
  if (ds.record_count_method === 'file_count') return `${n} 个文件`;
  return n;
}

/**
 * 真实中医知识数据导入中心（仅管理员）。
 *
 * 流程：扫描数据源 → 查看字段映射与真实样例 → 用户确认 → 创建导入任务 → 实时进度。
 * 扫描不会自动导入；导入条数受后端 IMPORT_MAX_RECORDS_PER_JOB 限制（安全阀）。
 */
export default function ImportCenterPage() {
  const queryClient = useQueryClient();
  const [active, setActive] = useState<ImportDataset | null>(null);
  const [targetType, setTargetType] = useState<string>('');
  const [kbId, setKbId] = useState<string | undefined>(undefined);
  const [limit, setLimit] = useState<number>(20);
  const [vectorize, setVectorize] = useState<boolean>(true);
  const [confirmed, setConfirmed] = useState<boolean>(false);
  const [failuresOpen, setFailuresOpen] = useState(false);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [includeKb, setIncludeKb] = useState(false);
  const [cleanupConfirm, setCleanupConfirm] = useState(false);

  const config = useQuery({ queryKey: ['import', 'config'], queryFn: fetchImportConfig });

  const scan = useQuery({
    queryKey: ['import', 'scan'],
    queryFn: () => scanImportDatasets(5),
    enabled: false, // 不自动扫描：由管理员显式触发
    staleTime: 60_000,
  });

  const kbs = useQuery({ queryKey: ['kb'], queryFn: fetchKnowledgeBases, staleTime: 60_000 });

  const job = useQuery({
    queryKey: ['import', 'job', 'latest'],
    queryFn: fetchLatestImportJob,
    refetchInterval: (q) =>
      q.state.data && ['pending', 'processing'].includes(q.state.data.status) ? 2000 : false,
  });

  const scanMutation = useMutation({
    mutationFn: () => scanImportDatasets(5),
    onSuccess: (data) => {
      queryClient.setQueryData(['import', 'scan'], data);
      message.success(`扫描完成：${data.datasets.length} 个数据集`);
    },
    onError: (err) => message.error(`扫描失败：${(err as Error).message}`),
  });

  const importMutation = useMutation({
    mutationFn: () =>
      createImportJob({
        dataset_id: active!.dataset_id,
        target_type: targetType as 'herb' | 'prescription' | 'theory' | 'literature' | 'document',
        // 资源类也可指定知识库：指定后会一并挂载 + 向量化，才能进入 RAG 检索链路
        kb_id: kbId ?? null,
        limit,
        vectorize,
        confirmed: true,
      }),
    onSuccess: () => {
      message.success('导入任务已创建，正在后台执行');
      setActive(null);
      setConfirmed(false);
      void queryClient.invalidateQueries({ queryKey: ['import', 'job'] });
    },
    onError: (err) => message.error(`创建任务失败：${(err as Error).message}`),
  });

  // ── 资源批量挂载 + 向量化 ────────────────────────────────────────────────
  const [rvType, setRvType] = useState<'herb' | 'prescription' | 'theory' | 'literature'>('herb');
  const [rvKbId, setRvKbId] = useState<string | undefined>(undefined);
  const [rvLimit, setRvLimit] = useState<number>(20);

  const rvStats = useQuery({
    queryKey: ['import', 'resource-vectorize', 'stats', rvKbId],
    queryFn: () => fetchResourceVectorizeStats(rvKbId),
    staleTime: 30_000,
  });

  const rvJobQuery = useQuery({
    queryKey: ['import', 'resource-vectorize', 'job', rvType],
    queryFn: () => fetchLatestResourceVectorizeJob(rvType),
    refetchInterval: (q) =>
      q.state.data && ['pending', 'processing'].includes(q.state.data.status) ? 2000 : false,
  });
  const rvJob = rvJobQuery.data ?? null;
  const rvBusy = !!rvJob && ['pending', 'processing'].includes(rvJob.status);
  // 失败记录弹窗的数据来源：null = 数据集导入任务，否则为资源批量任务
  const [failuresJob, setFailuresJob] = useState<ImportJobStatus | null>(null);

  const rvCreate = useMutation({
    mutationFn: () =>
      createResourceVectorizeJob({
        resource_type: rvType,
        kb_id: rvKbId!,
        limit: rvLimit,
      }),
    onSuccess: (job) => {
      message.success(`已创建资源批量任务：${job.total} 条`);
      void queryClient.invalidateQueries({ queryKey: ['import', 'resource-vectorize'] });
    },
    onError: (err) => message.error(`创建任务失败：${(err as Error).message}`),
  });

  const rvResume = useMutation({
    mutationFn: (jobId: string) => resumeResourceVectorizeJob(jobId),
    onSuccess: () => {
      message.success('已从数据库记录的断点继续');
      void queryClient.invalidateQueries({ queryKey: ['import', 'resource-vectorize'] });
    },
    onError: (err) => message.error(`继续任务失败：${(err as Error).message}`),
  });

  const cleanupStats = useQuery({
    queryKey: ['import', 'cleanup', 'stats'],
    queryFn: fetchCleanupStats,
    enabled: cleanupOpen,
    staleTime: 30_000,
  });

  const planMutation = useMutation({
    mutationFn: () => planCleanup({ include_test_data: true, include_knowledge_bases: includeKb }),
    onError: (err) => message.error(`生成计划失败：${(err as Error).message}`),
  });

  const executeMutation = useMutation({
    mutationFn: () =>
      executeCleanup({
        include_test_data: true,
        include_knowledge_bases: includeKb,
        confirm: true,
      }),
    onSuccess: () => {
      message.success('清理完成');
      setCleanupConfirm(false);
      void queryClient.invalidateQueries({ queryKey: ['import', 'cleanup'] });
    },
    onError: (err) => message.error(`清理失败：${(err as Error).message}`),
  });

  const cancelMutation = useMutation({
    mutationFn: (jobId: string) => cancelImportJob(jobId),
    onSuccess: () => {
      message.success('已提交取消请求');
      void queryClient.invalidateQueries({ queryKey: ['import', 'job'] });
    },
    onError: (err) => message.error(`取消失败：${(err as Error).message}`),
  });

  const datasets = scan.data?.datasets ?? [];
  const importable = useMemo(
    () => datasets.filter((d) => !['relation', 'unknown'].includes(d.detected_type)),
    [datasets],
  );

  const columns: ColumnsType<ImportDataset> = [
    { title: '数据集', dataIndex: 'name', width: 260, ellipsis: true },
    { title: '格式', dataIndex: 'format', width: 90 },
    {
      title: '大小',
      dataIndex: 'size_bytes',
      width: 110,
      render: (_v, r) => formatSize(r.size_bytes),
    },
    { title: '数据量', width: 150, render: (_v, r) => formatCount(r) },
    {
      title: '识别类型',
      dataIndex: 'detected_type',
      width: 170,
      render: (v: string) => <Tag color={v === 'unknown' ? 'red' : 'blue'}>{TYPE_LABEL[v] ?? v}</Tag>,
    },
    {
      title: '待确认字段',
      width: 110,
      render: (_v, r) =>
        r.pending_fields.length ? <Tag color="orange">{r.pending_fields.length}</Tag> : <Tag>0</Tag>,
    },
    {
      title: '操作',
      width: 120,
      render: (_v, r) => (
        <Button
          type="link"
          onClick={() => {
            setActive(r);
            setTargetType(r.detected_type);
            setConfirmed(false);
          }}
        >
          映射预览
        </Button>
      ),
    },
  ];

  const mappingColumns: ColumnsType<ImportFieldMapping> = [
    { title: '原始字段', dataIndex: 'source', width: 220, ellipsis: true },
    {
      title: '系统字段',
      dataIndex: 'target',
      width: 200,
      render: (v: string | null) => v ?? <Typography.Text type="secondary">—</Typography.Text>,
    },
    {
      title: '结论',
      dataIndex: 'status',
      width: 90,
      render: (v: string) => (
        <Tag color={STATUS_COLOR[v]}>
          {v === 'mapped' ? '可导入' : v === 'pending' ? '待确认' : '忽略'}
        </Tag>
      ),
    },
    { title: '说明', dataIndex: 'note' },
  ];

  const jobData: ImportJobStatus | null | undefined = job.data;
  const busy = ['pending', 'processing'].includes(jobData?.status ?? '');

  const headerRight = (
    <Space size="large" align="center">
      <Button
        type="primary"
        icon={<DatabaseOutlined />}
        loading={scanMutation.isPending}
        onClick={() => scanMutation.mutate()}
      >
        扫描数据源
      </Button>
      <Button
        icon={<ReloadOutlined />}
        onClick={() => void queryClient.invalidateQueries({ queryKey: ['import', 'job'] })}
      >
        刷新进度
      </Button>
      <AdminHeaderRight />
    </Space>
  );

  return (
    <AppLayout
      pageTitle=""
      headerLeft={
        <Space size="middle" align="center">
          <ImportOutlined style={{ fontSize: 20 }} />
          <h2 style={{ margin: 0 }}>知识数据导入中心</h2>
        </Space>
      }
      headerRight={headerRight}
    >
      {config.data && !config.data.configured && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="未配置数据源目录"
          description="请在后端 .env 中设置 IMPORT_SOURCE_DIR 后重启服务（后端直接读取本机目录，不通过浏览器上传大文件）。"
        />
      )}

      {config.data?.configured && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={`数据源目录：${config.data.source_dir}`}
          description={`单次导入上限 ${config.data.max_records_per_job} 条（安全阀，防止一次全量导入百万级语料）。扫描为只读分析，不会自动导入。`}
        />
      )}

      {/* 上传文件导入：自动识别类型 + 字段映射 + 预览确认 */}
      <UploadImportPanel />

      {/* 导入进度 */}
      {jobData && (
        <Card title="导入进度" style={{ marginBottom: 16 }}>
          <Row gutter={16}>
            <Col span={6}>
              <Statistic title="状态" value={JOB_STATUS_LABEL[jobData.status] ?? jobData.status} />
            </Col>
            <Col span={6}>
              <Statistic title="成功" value={jobData.succeeded} suffix={`/ ${jobData.total}`} />
            </Col>
            <Col span={6}>
              <Statistic title="跳过（空值/重复）" value={jobData.skipped} />
            </Col>
            <Col span={6}>
              <Statistic title="失败" value={jobData.failed} />
            </Col>
          </Row>
          <Progress
            percent={jobData.percent}
            status={jobData.status === 'failed' ? 'exception' : busy ? 'active' : 'success'}
            style={{ marginTop: 12 }}
          />
          <Space style={{ marginTop: 8 }} wrap>
            <Typography.Text type="secondary">
              数据集：{jobData.dataset_name} ｜ 目标：{TYPE_LABEL[jobData.target_type] ?? jobData.target_type} ｜
              批次：{jobData.batch_id}
            </Typography.Text>
            {jobData.failed > 0 && (
              <Button
                size="small"
                onClick={() => {
                  setFailuresJob(null);
                  setFailuresOpen(true);
                }}
              >
                查看失败记录
              </Button>
            )}
            {busy && (
              <Button
                size="small"
                danger
                loading={cancelMutation.isPending}
                onClick={() => cancelMutation.mutate(jobData.job_id)}
              >
                取消任务
              </Button>
            )}
          </Space>
          {jobData.error_message && (
            <Alert type="error" showIcon style={{ marginTop: 12 }} message={jobData.error_message.slice(0, 500)} />
          )}
        </Card>
      )}

      {/* 资源批量挂载 + 向量化（已有资源 → KnowledgeBaseResource → BGE-M3 → Milvus） */}
      <Card
        title="资源批量挂载与向量化"
        style={{ marginBottom: 16 }}
        extra={
          <Typography.Text type="secondary">
            每批 {rvJob?.batch_size ?? '—'} 条 · 单 worker 串行 · 支持断点续跑
          </Typography.Text>
        }
      >
        <Space wrap style={{ marginBottom: 12 }}>
          <span>资源类型：</span>
          <Select
            value={rvType}
            onChange={setRvType}
            style={{ width: 160 }}
            options={[
              { label: '中药', value: 'herb' },
              { label: '方剂', value: 'prescription' },
              { label: '中医理论', value: 'theory' },
              { label: '文献', value: 'literature' },
            ]}
          />
          <span>目标知识库：</span>
          <Select
            placeholder="必选（资源需挂载才能被检索）"
            value={rvKbId}
            onChange={setRvKbId}
            style={{ width: 240 }}
            options={(kbs.data ?? []).map((k) => ({ label: k.name, value: k.id }))}
          />
          <span>本批条数：</span>
          <InputNumber min={1} max={5000} value={rvLimit} onChange={(v) => setRvLimit(v ?? 20)} />
          <Button
            type="primary"
            icon={<DatabaseOutlined />}
            disabled={!rvKbId}
            loading={rvCreate.isPending}
            onClick={() => rvCreate.mutate()}
          >
            开始批量挂载
          </Button>
          {rvJob && !rvBusy && (rvJob.failed_items?.length ?? 0) > 0 && (
            <Button loading={rvResume.isPending} onClick={() => rvResume.mutate(rvJob.job_id)}>
              继续 / 重试未完成项（{rvJob.failed_items?.length}）
            </Button>
          )}
          {rvBusy && (
            <Button
              size="small"
              danger
              loading={cancelMutation.isPending}
              onClick={() => cancelMutation.mutate(rvJob!.job_id)}
            >
              停止任务
            </Button>
          )}
        </Space>

        <Table
          rowKey="resource_type"
          size="small"
          pagination={false}
          dataSource={rvStats.data?.items ?? []}
          columns={[
            { title: '资源类型', dataIndex: 'label', width: 120 },
            { title: '总数', dataIndex: 'total', width: 100 },
            { title: '已挂载', dataIndex: 'mounted', width: 100 },
            {
              title: '未挂载（待处理）',
              dataIndex: 'unmounted',
              width: 140,
              render: (v: number) => <Tag color={v > 0 ? 'orange' : 'green'}>{v}</Tag>,
            },
          ]}
          style={{ marginBottom: 12 }}
        />

        {rvJob && (
          <>
            <Row gutter={16}>
              <Col span={4}>
                <Statistic title="总数量" value={rvJob.total} />
              </Col>
              <Col span={4}>
                <Statistic title="已处理" value={rvJob.processed} />
              </Col>
              <Col span={4}>
                <Statistic title="成功" value={rvJob.succeeded} />
              </Col>
              <Col span={4}>
                <Statistic title="跳过（已完成）" value={rvJob.skipped} />
              </Col>
              <Col span={4}>
                <Statistic title="失败" value={rvJob.failed} />
              </Col>
              <Col span={4}>
                <Statistic title="处理中" value={rvJob.processing ?? 0} />
              </Col>
            </Row>
            <Progress
              percent={rvJob.percent}
              status={rvJob.status === 'failed' ? 'exception' : rvBusy ? 'active' : 'success'}
              style={{ marginTop: 12 }}
            />
            <Space style={{ marginTop: 8 }} wrap>
              <Typography.Text type="secondary">
                任务类型：资源批量挂载（{TYPE_LABEL[rvJob.target_type] ?? rvJob.target_type}） ｜
                当前批次：{rvJob.current_batch ?? 0}/{rvJob.total_batches ?? 0} ｜ 状态：
                {JOB_STATUS_LABEL[rvJob.status] ?? rvJob.status}
              </Typography.Text>
              {rvJob.failed > 0 && (
                <Button
                  size="small"
                  onClick={() => {
                    setFailuresJob(rvJob);
                    setFailuresOpen(true);
                  }}
                >
                  查看失败记录
                </Button>
              )}
            </Space>
            {rvJob.error_message && (
              <Alert
                type="error"
                showIcon
                style={{ marginTop: 12 }}
                message={rvJob.error_message.slice(0, 500)}
              />
            )}
          </>
        )}
        {!rvJob && (
          <Typography.Text type="secondary">
            说明：资源（中药/方剂/理论/文献）必须挂载到知识库并写入 BGE-M3 向量后才会进入检索链路。
            任务按批执行、进度落库，中断后用「继续 / 重试未完成项」从断点继续，已成功的条目不会重复向量化。
          </Typography.Text>
        )}
      </Card>

      {/* 测试数据清理 */}
      <Card
        title="测试数据清理"
        style={{ marginBottom: 16 }}
        extra={
          <Button onClick={() => setCleanupOpen((v) => !v)}>
            {cleanupOpen ? '收起' : '展开统计'}
          </Button>
        }
      >
        {!cleanupOpen ? (
          <Typography.Text type="secondary">
            区分规则：import_batch_id 非空 = 真实导入数据；为空 = 手工/历史测试数据。清理不会触碰用户、权限、系统配置与审计。
          </Typography.Text>
        ) : (
          <>
            {cleanupStats.isLoading && <Typography.Text>统计中…</Typography.Text>}
            {cleanupStats.data && (
              <Descriptions bordered size="small" column={3} style={{ marginBottom: 12 }}>
                <Descriptions.Item label="知识库（总/含真实数据/仅测试）">
                  {cleanupStats.data.knowledge_bases.total} /{' '}
                  {cleanupStats.data.knowledge_bases.with_imported_data} /{' '}
                  {cleanupStats.data.knowledge_bases.test_only}
                </Descriptions.Item>
                <Descriptions.Item label="文档（总/真实/测试）">
                  {cleanupStats.data.documents.total} / {cleanupStats.data.documents.imported} /{' '}
                  {cleanupStats.data.documents.test}
                </Descriptions.Item>
                <Descriptions.Item label="切片（总/真实/测试）">
                  {cleanupStats.data.chunks.total} / {cleanupStats.data.chunks.imported} /{' '}
                  {cleanupStats.data.chunks.test}
                </Descriptions.Item>
                {Object.entries(cleanupStats.data.resources).map(([k, v]) => (
                  <Descriptions.Item key={k} label={`${TYPE_LABEL[k] ?? k}（总/真实/测试）`}>
                    {v.total} / {v.imported} / {v.test}
                  </Descriptions.Item>
                ))}
                <Descriptions.Item label="Milvus 向量总数">
                  {cleanupStats.data.milvus_vectors.total ?? '不可用'}
                </Descriptions.Item>
              </Descriptions>
            )}
            <Space wrap>
              <Checkbox
                checked={includeKb}
                onChange={(e) => setIncludeKb(e.target.checked)}
              >
                同时删除「仅含测试数据」的知识库
              </Checkbox>
              <Button
                loading={planMutation.isPending}
                onClick={() => planMutation.mutate()}
                disabled={!cleanupStats.data}
              >
                生成清理计划（只读）
              </Button>
            </Space>
            {planMutation.data && (
              <Alert
                style={{ marginTop: 12 }}
                type="warning"
                showIcon
                message={`计划删除：文档 ${planMutation.data.documents}、切片 ${planMutation.data.chunks}、知识库 ${planMutation.data.knowledge_bases}、资源 ${Object.entries(
                  planMutation.data.resources,
                )
                  .map(([k, v]) => `${TYPE_LABEL[k] ?? k} ${v}`)
                  .join('、')}`}
                description={`受保护不删除：${planMutation.data.protected.join('、')}`}
              />
            )}
            <Space style={{ marginTop: 12 }} wrap>
              <Checkbox
                checked={cleanupConfirm}
                onChange={(e) => setCleanupConfirm(e.target.checked)}
              >
                我已确认上述范围，执行清理（不可撤销）
              </Checkbox>
              <Popconfirm
                title="确认执行清理？"
                description="将按上述范围删除文档/切片/资源/向量，用户与审计不受影响。"
                okText="确认删除"
                cancelText="取消"
                onConfirm={() => executeMutation.mutate()}
              >
                <Button danger disabled={!cleanupConfirm} loading={executeMutation.isPending}>
                  执行清理
                </Button>
              </Popconfirm>
            </Space>
            {executeMutation.data && (
              <Alert
                style={{ marginTop: 12 }}
                type={executeMutation.data.error_count ? 'warning' : 'success'}
                showIcon
                message={`清理完成：${Object.entries(executeMutation.data.deleted)
                  .map(([k, v]) => `${k}=${v}`)
                  .join('，')}`}
                description={
                  executeMutation.data.errors.length
                    ? executeMutation.data.errors.slice(0, 3).join('；')
                    : undefined
                }
              />
            )}
          </>
        )}
      </Card>

      <Card title="数据集列表">
        {!scan.data ? (
          <Empty description="点击右上角「扫描数据源」开始只读扫描" />
        ) : (
          <>
            <Table
              rowKey="dataset_id"
              columns={columns}
              dataSource={datasets}
              pagination={false}
              size="small"
            />
            {scan.data.skipped.length > 0 && (
              <Alert
                type="info"
                showIcon
                style={{ marginTop: 12 }}
                message={`已跳过 ${scan.data.skipped.length} 项（不支持的格式或说明文件）`}
                description={scan.data.skipped.slice(0, 5).join('；')}
              />
            )}
          </>
        )}
      </Card>

      {/* 映射预览 + 确认导入 */}
      <Drawer
        open={!!active}
        onClose={() => setActive(null)}
        width={860}
        title={active ? `映射预览：${active.name}` : '映射预览'}
      >
        {active && (
          <>
            <Descriptions column={2} size="small" bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label="格式">{active.format}</Descriptions.Item>
              <Descriptions.Item label="大小">{formatSize(active.size_bytes)}</Descriptions.Item>
              <Descriptions.Item label="数据量">{formatCount(active)}</Descriptions.Item>
              <Descriptions.Item label="识别类型">
                <Tag color={active.detected_type === 'unknown' ? 'red' : 'blue'}>
                  {TYPE_LABEL[active.detected_type] ?? active.detected_type}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="路径" span={2}>
                {active.relative_path}
              </Descriptions.Item>
              <Descriptions.Item label="判定依据" span={2}>
                {active.detected_reason}
              </Descriptions.Item>
            </Descriptions>

            {active.warnings.map((w) => (
              <Alert key={w} type="warning" showIcon style={{ marginBottom: 8 }} message={w} />
            ))}

            <Typography.Title level={5}>字段映射</Typography.Title>
            <Table
              rowKey={(_r, i) => `${_r.source}-${i}`}
              columns={mappingColumns}
              dataSource={active.field_mappings}
              pagination={false}
              size="small"
              style={{ marginBottom: 16 }}
            />

            <Typography.Title level={5}>真实数据预览（前 {active.samples.length} 条）</Typography.Title>
            <pre
              style={{
                maxHeight: 240,
                overflow: 'auto',
                background: '#fafafa',
                padding: 12,
                fontSize: 12,
              }}
            >
              {JSON.stringify(active.samples, null, 2)}
            </pre>

            <Typography.Title level={5}>导入设置</Typography.Title>
            <Space direction="vertical" style={{ width: '100%' }}>
              <Space wrap>
                <span>导入为：</span>
                <Select
                  value={targetType}
                  onChange={setTargetType}
                  style={{ width: 200 }}
                  options={Object.entries(TYPE_LABEL)
                    .filter(([k]) => importable.some((d) => d.detected_type === k) || k === 'document')
                    .map(([k, v]) => ({ label: v, value: k }))}
                />
                {targetType !== '' && (
                  <Select
                    allowClear
                    placeholder={
                      targetType === 'document'
                        ? '选择目标知识库（必选）'
                        : '选择知识库则一并挂载并向量化（资源需挂载才能被检索到）'
                    }
                    value={kbId}
                    onChange={setKbId}
                    style={{ width: 240 }}
                    options={(kbs.data ?? []).map((k) => ({ label: k.name, value: k.id }))}
                  />
                )}
                <span>条数：</span>
                <InputNumber min={1} max={200} value={limit} onChange={(v) => setLimit(v ?? 20)} />
              </Space>
              {targetType === 'document' && (
                <Checkbox checked={vectorize} onChange={(e) => setVectorize(e.target.checked)}>
                  导入后立即向量化（走现有 BGE-M3 → Milvus 链路，耗时较长）
                </Checkbox>
              )}
              {active.pending_fields.length > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  message={`${active.pending_fields.length} 个字段标记为「待确认」，导入时不会写入：${active.pending_fields.join('、')}`}
                />
              )}
              <Checkbox
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
                disabled={active.detected_type === 'relation' || active.detected_type === 'unknown'}
              >
                我已确认字段映射与导入范围，开始导入（不会自动导入）
              </Checkbox>
              <Space>
                <Button
                  type="primary"
                  icon={<CloudUploadOutlined />}
                  disabled={!confirmed || !targetType || (targetType === 'document' && !kbId)}
                  loading={importMutation.isPending}
                  onClick={() => importMutation.mutate()}
                >
                  开始导入
                </Button>
                <Button onClick={() => setActive(null)}>取消</Button>
              </Space>
            </Space>
          </>
        )}
      </Drawer>

      <Modal
        open={failuresOpen}
        onCancel={() => setFailuresOpen(false)}
        footer={null}
        title="失败记录"
        width={760}
      >
        <Table
          rowKey={(r, i) => `${r.resource_id ?? r.index ?? i}`}
          size="small"
          pagination={false}
          dataSource={(failuresJob ?? jobData)?.failed_items ?? []}
          columns={[
            { title: '序号 / 资源ID', dataIndex: 'resource_id', width: 320, ellipsis: true,
              render: (_v: unknown, r) => r.resource_id ?? r.index ?? '—' },
            { title: '名称', dataIndex: 'name', width: 180, ellipsis: true },
            { title: '原因', dataIndex: 'reason' },
          ]}
        />
      </Modal>
    </AppLayout>
  );
}
