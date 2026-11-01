'use client';

import { ReloadOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Divider,
  Empty,
  Modal,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { AdminHeaderRight } from '@/components/layout/AppSider';
import { AppLayout } from '@/components/layout/AppLayout';
import {
  applyIndexFix,
  fetchIndexDiff,
  fetchIndexEnvironment,
  fetchKnowledgeBases,
  rebuildIndex,
  type IndexDiffDocument,
  type IndexDiffReport,
  type IndexEnvironment,
} from '@/services/api';
import {
  buildFixPreview,
  buildRebuildPreview,
  canDeleteVectors,
  documentIssueTags,
  environmentMatches,
  indexStatusLabel,
  type IndexFixPlan,
} from '@/utils/indexConsistency';

type PendingAction =
  | { type: 'fix'; plan: IndexFixPlan }
  | { type: 'rebuild' }
  | { type: 'retry' };

interface OperationResult {
  title: string;
  lines: string[];
}

export default function IndexConsistencyPage() {
  const [kbId, setKbId] = useState<string>();
  const [report, setReport] = useState<IndexDiffReport | null>(null);
  const [reportCollection, setReportCollection] = useState('');
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState('');
  const [fixFlags, setFixFlags] = useState(true);
  const [deleteOrphans, setDeleteOrphans] = useState(false);
  const [deleteDuplicates, setDeleteDuplicates] = useState(false);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [destructiveAck, setDestructiveAck] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<OperationResult | null>(null);

  const {
    data: env,
    isLoading: envLoading,
    error: envError,
    refetch: refetchEnv,
  } = useQuery({
    queryKey: ['index-environment'],
    queryFn: fetchIndexEnvironment,
    staleTime: 30_000,
  });

  const { data: kbs = [], isLoading: kbsLoading } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: fetchKnowledgeBases,
  });

  const deleteAllowed = canDeleteVectors(env ?? null);
  const reportEnvMatches = Boolean(
    report && reportCollection && environmentMatches(env ?? null, reportCollection),
  );

  const plan: IndexFixPlan = useMemo(
    () => ({ fix_flags: fixFlags, delete_orphans: deleteOrphans, delete_duplicates: deleteDuplicates }),
    [fixFlags, deleteOrphans, deleteDuplicates],
  );

  const fixPreview = useMemo(
    () => (report ? buildFixPreview(report, plan) : null),
    [report, plan],
  );
  const rebuildPreview = useMemo(() => (report ? buildRebuildPreview(report) : null), [report]);

  const loadDiff = async () => {
    if (!kbId) {
      message.warning('请先选择知识库');
      return;
    }
    if (!env) {
      message.error('未获取到当前索引环境，已阻止检查');
      return;
    }
    setDiffLoading(true);
    setDiffError('');
    setResult(null);
    try {
      const data = await fetchIndexDiff(kbId, env.milvus_collection);
      setReport(data);
      setReportCollection(env.milvus_collection);
      setDeleteOrphans(false);
      setDeleteDuplicates(false);
      if (!data.clean) {
        message.warning('检查完成：发现索引不一致，请查看明细后再决定是否修复');
      } else {
        message.success('检查完成：当前知识库索引一致');
      }
    } catch (error) {
      setReport(null);
      setReportCollection('');
      setDiffError((error as Error).message);
      message.error(`检查失败：${(error as Error).message}`);
      void refetchEnv();
    } finally {
      setDiffLoading(false);
    }
  };

  const openAction = (action: PendingAction) => {
    if (!report || !reportEnvMatches) {
      message.error('报告已过期或环境已变化，请重新执行只读检查');
      return;
    }
    setDestructiveAck(false);
    setPending(action);
  };

  const pendingWantsDelete =
    pending?.type === 'fix' && (pending.plan.delete_orphans || pending.plan.delete_duplicates);
  const pendingPreview =
    pending?.type === 'fix' && report
      ? buildFixPreview(report, pending.plan)
      : pending?.type === 'rebuild' || pending?.type === 'retry'
        ? rebuildPreview
        : null;
  const ackRequired = Boolean(
    pendingWantsDelete &&
      fixPreview &&
      fixPreview.orphan_vectors + fixPreview.duplicate_vectors > 0,
  );

  const runPending = async () => {
    if (!pending || !kbId || !env) return;
    if (!reportEnvMatches) {
      message.error('环境已变化，请重新检查后再操作');
      setPending(null);
      return;
    }
    if (ackRequired && !destructiveAck) {
      message.warning('请确认已了解删除向量不可恢复');
      return;
    }
    setRunning(true);
    try {
      if (pending.type === 'fix') {
        const data = await applyIndexFix({
          kb_id: kbId,
          ...pending.plan,
          confirmed: pending.plan.delete_orphans || pending.plan.delete_duplicates,
          expected_milvus_collection: reportCollection,
        });
        setResult({
          title: '修复完成',
          lines: [
            `修正状态标记：${data.flags_updated} 个文档`,
            `删除孤儿向量：${data.orphans_deleted} 条`,
            `删除重复向量：${data.duplicates_deleted} 条`,
            `跳过删除：${data.skipped_deletions} 条`,
          ],
        });
        message.success('修复完成，已重新检查索引差异');
      } else if (pending.type === 'rebuild') {
        const data = await rebuildIndex({
          kb_id: kbId,
          retry_failed: false,
          expected_milvus_collection: reportCollection,
        });
        setResult({
          title: '已标记重建',
          lines: [
            `待重建文档：${data.rebuild_marked ?? 0} 个`,
            '仅重置索引状态，向量由既有向量化任务重建',
          ],
        });
        message.success(`已标记 ${data.rebuild_marked ?? 0} 个文档重建`);
      } else {
        const data = await rebuildIndex({
          kb_id: kbId,
          retry_failed: true,
          expected_milvus_collection: reportCollection,
        });
        setResult({
          title: '已重置失败文档',
          lines: [
            `重置失败文档：${data.retry_failed ?? 0} 个`,
            '错误信息已清空，将由既有向量化任务重试',
          ],
        });
        message.success(`已重置 ${data.retry_failed ?? 0} 个失败文档`);
      }
      setPending(null);
      setDeleteOrphans(false);
      setDeleteDuplicates(false);
      if (kbId && env) {
        try {
          const data = await fetchIndexDiff(kbId, env.milvus_collection);
          setReport(data);
          setReportCollection(env.milvus_collection);
        } catch (error) {
          message.warning(`操作已完成，但刷新差异报告失败：${(error as Error).message}`);
          void refetchEnv();
        }
      }
    } catch (error) {
      message.error(`操作失败：${(error as Error).message}`);
    } finally {
      setRunning(false);
    }
  };

  const columns: ColumnsType<IndexDiffDocument> = [
    { title: '文档', dataIndex: 'file_name', ellipsis: true },
    {
      title: '分片/向量',
      width: 120,
      render: (_, row) => `${row.expected_chunks} / ${row.actual_vectors}`,
    },
    {
      title: '问题',
      width: 200,
      render: (_, row) => {
        const tags = documentIssueTags(row);
        return tags.length === 0 ? (
          <Tag color="green">一致</Tag>
        ) : (
          <Space wrap size={[4, 4]}>
            {tags.map((tag) => (
              <Tag key={tag} color={tag.startsWith('状态') ? 'orange' : 'red'}>
                {tag}
              </Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: '索引状态',
      width: 170,
      render: (_, row) => (
        <Space size={4}>
          <Tag>{indexStatusLabel(row.current_status)}</Tag>
          {row.flag_inaccurate && (
            <>
              <Typography.Text type="secondary">→</Typography.Text>
              <Tag color="orange">{indexStatusLabel(row.suggested_status)}</Tag>
            </>
          )}
        </Space>
      ),
    },
  ];

  return (
    <AppLayout headerRight={<AdminHeaderRight />}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div>
          <Typography.Title level={4} style={{ marginBottom: 4 }}>
            <SafetyCertificateOutlined /> 索引一致性检查
          </Typography.Title>
          <Typography.Text type="secondary">
            比对 PostgreSQL 中的分片记录与 Milvus 实际向量。检查本身只读；修复需要先预览影响范围并二次确认，
            删除向量默认关闭且仅在隔离环境可用。
          </Typography.Text>
        </div>

        <Card title="当前索引环境">
          {envError ? (
            <Alert
              type="error"
              showIcon
              message="无法获取索引环境，已阻止后续操作"
              description={(envError as Error).message}
              action={
                <Button size="small" onClick={() => void refetchEnv()}>
                  重试
                </Button>
              }
            />
          ) : envLoading || !env ? (
            <Typography.Text type="secondary">正在读取后端环境信息…</Typography.Text>
          ) : (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Space wrap>
                <Tag color={env.env === 'prod' ? 'red' : 'blue'}>{env.env || 'unknown'}</Tag>
                <Typography.Text>
                  数据库：<Typography.Text code>{env.database || '—'}</Typography.Text>
                </Typography.Text>
                <Typography.Text>
                  Milvus 集合：<Typography.Text code>{env.milvus_collection || '—'}</Typography.Text>
                </Typography.Text>
                <Tag color={env.isolated_collection ? 'green' : 'red'}>
                  {env.isolated_collection ? '隔离集合' : '正式/非隔离集合'}
                </Tag>
                <Tag color={env.destructive_allowed ? 'green' : 'red'}>
                  {env.destructive_allowed ? '允许删除向量' : '禁止删除向量'}
                </Tag>
                <Button size="small" onClick={() => void refetchEnv()}>
                  刷新环境
                </Button>
              </Space>
              {!env.isolated_collection && (
                <Alert
                  type="warning"
                  showIcon
                  message="当前绑定的是正式/非隔离集合"
                  description="本页只允许只读检查、状态标记修复与重建标记；删除孤儿/重复向量会被前端禁用且后端拒绝。所有请求都绑定上面的集合名，集合发生变化时会直接报错而不是跨环境执行。"
                />
              )}
            </Space>
          )}
        </Card>

        <Card
          title="1. 只读差异检查"
          extra={
            <Button
              icon={<ReloadOutlined />}
              loading={diffLoading}
              disabled={!kbId || !env}
              onClick={() => void loadDiff()}
            >
              开始检查
            </Button>
          }
        >
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space wrap>
              <Typography.Text>知识库：</Typography.Text>
              <Select
                style={{ minWidth: 300 }}
                placeholder="请选择知识库（必选）"
                value={kbId}
                loading={kbsLoading}
                showSearch
                optionFilterProp="label"
                onChange={(value: string) => {
                  setKbId(value || undefined);
                  setReport(null);
                  setReportCollection('');
                  setResult(null);
                  setDiffError('');
                }}
                options={kbs.map((kb) => ({ label: kb.name, value: kb.id }))}
                aria-label="选择知识库"
              />
              <Typography.Text type="secondary">
                仅显示当前账号可访问的知识库；服务端会再次校验读权限。
              </Typography.Text>
            </Space>

            {diffError && (
              <Alert type="error" showIcon message="检查失败" description={diffError} />
            )}

            {!report ? (
              <Empty description="选择知识库后执行只读检查，不会修改任何数据" />
            ) : (
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                {!reportEnvMatches && (
                  <Alert
                    type="warning"
                    showIcon
                    message="报告绑定的集合与当前环境不一致"
                    description={`报告基于 ${reportCollection}，当前后端为 ${env?.milvus_collection ?? '未知'}。请重新检查后再操作。`}
                  />
                )}
                {!report.milvus_available && (
                  <Alert
                    type="error"
                    showIcon
                    message="Milvus 不可用，报告不完整"
                    description={report.error || '无法查询 Milvus 向量'}
                  />
                )}
                <Space size="large" wrap>
                  <Statistic title="扫描文档" value={report.scanned_documents} />
                  <Statistic
                    title="缺失向量"
                    value={report.totals.missing_vectors}
                    valueStyle={{ color: report.totals.missing_vectors ? '#cf1322' : '#3f8600' }}
                  />
                  <Statistic
                    title="孤儿向量"
                    value={report.totals.orphan_vectors}
                    valueStyle={{ color: report.totals.orphan_vectors ? '#cf1322' : '#3f8600' }}
                  />
                  <Statistic
                    title="重复向量"
                    value={report.totals.duplicate_vectors}
                    valueStyle={{ color: report.totals.duplicate_vectors ? '#cf1322' : '#3f8600' }}
                  />
                  <Statistic
                    title="状态异常"
                    value={report.totals.inaccurate_flags}
                    valueStyle={{ color: report.totals.inaccurate_flags ? '#fa8c16' : '#3f8600' }}
                  />
                  <Statistic
                    title="结论"
                    value={report.clean ? '一致' : '存在差异'}
                    valueStyle={{ color: report.clean ? '#3f8600' : '#cf1322' }}
                  />
                </Space>

                <Table
                  size="small"
                  rowKey="doc_id"
                  loading={diffLoading}
                  dataSource={report.documents}
                  columns={columns}
                  pagination={{ pageSize: 10, size: 'small' }}
                  locale={{ emptyText: '当前知识库没有文档' }}
                  expandable={{
                    expandedRowRender: (row) => (
                      <Space direction="vertical" size={4}>
                        <Typography.Text strong>缺失分片 ID</Typography.Text>
                        {row.missing_chunk_ids.length === 0 ? (
                          <Typography.Text type="secondary">无</Typography.Text>
                        ) : (
                          <Space wrap size={[4, 4]}>
                            {row.missing_chunk_ids.map((id) => (
                              <Tag key={id}>{id}</Tag>
                            ))}
                          </Space>
                        )}
                        {row.missing_count > row.missing_chunk_ids.length && (
                          <Typography.Text type="secondary">
                            仅展示前 {row.missing_chunk_ids.length} 条（共 {row.missing_count} 条）
                          </Typography.Text>
                        )}
                        <Typography.Text strong>孤儿向量 ID</Typography.Text>
                        {row.orphan_vector_ids.length === 0 ? (
                          <Typography.Text type="secondary">无</Typography.Text>
                        ) : (
                          <Space wrap size={[4, 4]}>
                            {row.orphan_vector_ids.map((id) => (
                              <Tag key={id}>{id}</Tag>
                            ))}
                          </Space>
                        )}
                        {row.orphan_count > row.orphan_vector_ids.length && (
                          <Typography.Text type="secondary">
                            仅展示前 {row.orphan_vector_ids.length} 条（共 {row.orphan_count} 条）
                          </Typography.Text>
                        )}
                        <Typography.Text strong>重复向量 ID</Typography.Text>
                        {row.duplicate_vector_ids.length === 0 ? (
                          <Typography.Text type="secondary">无</Typography.Text>
                        ) : (
                          <Space wrap size={[4, 4]}>
                            {row.duplicate_vector_ids.map((id) => (
                              <Tag key={id}>{id}</Tag>
                            ))}
                          </Space>
                        )}
                      </Space>
                    ),
                  }}
                />
              </Space>
            )}
          </Space>
        </Card>

        <Card title="2. 修复与重建">
          {!report ? (
            <Empty description="先执行只读检查，再选择修复操作" />
          ) : (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Descriptions
                size="small"
                bordered
                column={1}
                title="状态标记修复"
                items={[
                  {
                    key: 'flags',
                    label: (
                      <Checkbox
                        checked={fixFlags}
                        onChange={(e) => setFixFlags(e.target.checked)}
                      >
                        修正不准确的索引状态标记
                      </Checkbox>
                    ),
                    children: `预计修正 ${report.totals.inaccurate_flags} 个文档：只更新 PostgreSQL 的索引状态与向量计数，不删除向量`,
                  },
                ]}
              />

              <Divider orientation="left" plain>
                删除向量（默认关闭，不可恢复）
              </Divider>
              <Space direction="vertical" size="small" style={{ width: '100%' }}>
                <Tooltip
                  title={
                    deleteAllowed
                      ? undefined
                      : '当前集合不是隔离环境（*_dev / *_test），禁止删除向量'
                  }
                >
                  <Space direction="vertical">
                    <Checkbox
                      checked={deleteOrphans}
                      disabled={!deleteAllowed || report.totals.orphan_vectors === 0}
                      onChange={(e) => setDeleteOrphans(e.target.checked)}
                    >
                      删除孤儿向量（{report.totals.orphan_vectors} 条）
                    </Checkbox>
                    <Checkbox
                      checked={deleteDuplicates}
                      disabled={!deleteAllowed || report.totals.duplicate_vectors === 0}
                      onChange={(e) => setDeleteDuplicates(e.target.checked)}
                    >
                      删除重复向量（{report.totals.duplicate_vectors} 条）
                    </Checkbox>
                  </Space>
                </Tooltip>
                {!deleteAllowed && (
                  <Alert
                    type="warning"
                    showIcon
                    message="删除向量已禁用"
                    description="只有隔离集合（*_dev / *_test）且非生产环境才允许删除；后端会独立校验环境与确认标记。"
                  />
                )}
                {deleteAllowed && (deleteOrphans || deleteDuplicates) && (
                  <Alert
                    type="error"
                    showIcon
                    message="删除向量不可恢复"
                    description="孤儿/重复向量不属于任何有效分片，删除后无法恢复；执行前需要二次确认。"
                  />
                )}
              </Space>

              {fixPreview && (
                <Alert
                  type="info"
                  showIcon
                  message="影响范围预览"
                  description={
                    <ul style={{ margin: 0, paddingLeft: 18 }}>
                      {fixPreview.summary.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                      <li>受影响文档：{fixPreview.affected_documents} 个</li>
                    </ul>
                  }
                />
              )}

              <Space wrap>
                <Button
                  type="primary"
                  disabled={!reportEnvMatches}
                  onClick={() => openAction({ type: 'fix', plan })}
                >
                  执行修复（先预览确认）
                </Button>
                <Button
                  disabled={!reportEnvMatches}
                  onClick={() => openAction({ type: 'rebuild' })}
                >
                  标记缺失文档重建
                </Button>
                <Button
                  disabled={!reportEnvMatches}
                  onClick={() => openAction({ type: 'retry' })}
                >
                  重置失败文档重试
                </Button>
              </Space>

              {running && (
                <Alert
                  type="info"
                  showIcon
                  message="正在执行，请勿关闭页面"
                  description="操作完成后会自动刷新差异报告。删除向量为不可逆操作，请等待结果返回。"
                />
              )}

              <Alert
                type="info"
                showIcon
                message="重建说明"
                description="重建只把文档状态重置为「未索引」，不会直接编码；实际向量化仍由既有任务执行。"
              />

              {result && (
                <Alert
                  type="success"
                  showIcon
                  message={result.title}
                  description={
                    <ul style={{ margin: 0, paddingLeft: 18 }}>
                      {result.lines.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  }
                />
              )}
            </Space>
          )}
        </Card>

        <Modal
          open={pending !== null}
          title={
            pending?.type === 'fix'
              ? '确认执行索引修复'
              : pending?.type === 'rebuild'
                ? '确认标记重建'
                : '确认重置失败文档'
          }
          okText="确认执行"
          cancelText="取消"
          confirmLoading={running}
          onOk={() => void runPending()}
          onCancel={() => {
            if (!running) setPending(null);
          }}
          okButtonProps={{
            danger: Boolean(pendingWantsDelete),
            disabled: ackRequired && !destructiveAck,
          }}
        >
          {pendingPreview && (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Alert
                type={pendingWantsDelete ? 'error' : 'info'}
                showIcon
                message="本次操作影响范围"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {pendingPreview.summary.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                }
              />
              <Typography.Text type="secondary">
                目标环境：{env?.env}｜集合：{reportCollection}｜知识库已由服务端校验读权限
              </Typography.Text>
              {ackRequired && (
                <Checkbox
                  checked={destructiveAck}
                  onChange={(e) => setDestructiveAck(e.target.checked)}
                >
                  我已了解删除的向量不可恢复，并确认在隔离环境执行
                </Checkbox>
              )}
            </Space>
          )}
        </Modal>
      </Space>
    </AppLayout>
  );
}
