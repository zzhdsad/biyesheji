'use client';

import { ApartmentOutlined, ImportOutlined, SearchOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Divider,
  Empty,
  Input,
  Progress,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

import { AdminHeaderRight } from '@/components/layout/AppSider';
import { AppLayout } from '@/components/layout/AppLayout';
import ErDiagram, { type ErRelation, type ErTable } from '@/components/admin/ErDiagram';
import FilePicker, { type PickedFile } from '@/components/admin/FilePicker';
import {
  analyzeRelationalFiles,
  commitRelationalImport,
  fetchImportStats,
  fetchKnowledgeBases,
  fetchRelations,
  setRelationsStatus,
  type ForeignKeyItem,
  type JunctionItem,
  type RelationRow,
  type RelationalTable,
} from '@/services/api';

const STATUS_COLOR: Record<string, string> = {
  pending: 'orange',
  confirmed: 'green',
  rejected: 'red',
};
const STATUS_LABEL: Record<string, string> = {
  pending: '待确认',
  confirmed: '已确认',
  rejected: '已拒绝',
};

export default function RelationalImportPage() {
  const [picked, setPicked] = useState<PickedFile[]>([]);
  const [kbId, setKbId] = useState<string>();
  const [analyzing, setAnalyzing] = useState(false);
  const [progress, setProgress] = useState(0);
  const [sessionId, setSessionId] = useState('');
  const [tables, setTables] = useState<RelationalTable[]>([]);
  const [primaryKeys, setPrimaryKeys] = useState<Record<string, string>>({});
  const [foreignKeys, setForeignKeys] = useState<ForeignKeyItem[]>([]);
  const [junctions, setJunctions] = useState<JunctionItem[]>([]);
  const [analyzeErrors, setAnalyzeErrors] = useState<string[]>([]);
  const [selectedFks, setSelectedFks] = useState<number[]>([]);
  const [selectedJunctions, setSelectedJunctions] = useState<number[]>([]);
  const [relationTypes, setRelationTypes] = useState<Record<string, string>>({});
  const [autoConfirm, setAutoConfirm] = useState(false);
  const [committing, setCommitting] = useState(false);
  const [relations, setRelations] = useState<RelationRow[]>([]);
  const [relationFilter, setRelationFilter] = useState<string>('');
  const [selectedRelationIds, setSelectedRelationIds] = useState<string[]>([]);
  const [updatingRelations, setUpdatingRelations] = useState(false);
  const [commitResult, setCommitResult] = useState<{
    records: number;
    entities: number;
    relations: number;
    kg_nodes: number;
    kg_edges: number;
    failed_rows: number;
    errors: { file_name: string; row_no: number; message: string }[];
  }>();

  const { data: kbs = [] } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: fetchKnowledgeBases,
  });
  const { data: stats, refetch: refetchStats } = useQuery({
    queryKey: ['import-stats', kbId],
    queryFn: () => fetchImportStats(kbId as string),
    enabled: Boolean(kbId),
  });

  const runAnalyze = async () => {
    if (picked.length === 0) {
      message.warning('请先选择文件或文件夹');
      return;
    }
    setAnalyzing(true);
    setProgress(20);
    try {
      const data = await analyzeRelationalFiles(
        picked.map((p) => p.file),
        picked.map((p) => p.path),
      );
      setSessionId(data.session_id);
      setTables(data.tables);
      setForeignKeys(data.foreign_keys);
      setJunctions(data.junctions);
      setAnalyzeErrors(data.errors);
      // 默认采用置信度最高的主键候选，用户可改
      const pks: Record<string, string> = {};
      Object.entries(data.primary_keys).forEach(([table, fields]) => {
        if (fields.length > 0) pks[table] = fields[0];
      });
      setPrimaryKeys(pks);
      setSelectedFks(data.foreign_keys.map((_, index) => index));
      setSelectedJunctions(data.junctions.map((_, index) => index));
      setProgress(100);
      message.success(
        `分析完成：${data.tables.length} 张表，${data.foreign_keys.length} 个外键候选，${data.junctions.length} 个关联表`,
      );
    } catch (error) {
      message.error(`分析失败：${(error as Error).message}`);
      setProgress(0);
    } finally {
      setAnalyzing(false);
    }
  };

  const runCommit = async () => {
    if (!kbId) {
      message.warning('请选择目标知识库');
      return;
    }
    setCommitting(true);
    try {
      const data = await commitRelationalImport({
        session_id: sessionId,
        kb_id: kbId,
        primary_keys: primaryKeys,
        foreign_keys: selectedFks.map((index) => {
          const fk = foreignKeys[index];
          return {
            child_table: fk.child_table,
            child_field: fk.child_field,
            parent_table: fk.parent_table,
            parent_field: fk.parent_field,
            relation_type: relationTypes[`fk-${index}`] ?? 'related_to',
          };
        }),
        junctions: selectedJunctions.map((index) => {
          const junction = junctions[index];
          return {
            table: junction.table,
            left_table: junction.left_table,
            left_field: junction.left_field,
            right_table: junction.right_table,
            right_field: junction.right_field,
            relation_type: relationTypes[`jn-${index}`] ?? junction.relation_type,
          };
        }),
        auto_confirm: autoConfirm,
      });
      setCommitResult(data);
      refetchStats();
      // 必须同时刷新关系列表：导入后关系状态（pending/confirmed）已变化，
      // 只刷新统计会让「关系确认」区仍显示空表（浏览器验收时实测到）。
      await loadRelations();
      message.success(
        `导入完成：实体 ${data.entities}、关系 ${data.relations}、图谱边 ${data.kg_edges}`,
      );
    } catch (error) {
      message.error(`导入失败：${(error as Error).message}`);
    } finally {
      setCommitting(false);
    }
  };

  const loadRelations = useCallback(async () => {
    if (!kbId) return;
    const rows = await fetchRelations(kbId, relationFilter || undefined, 200);
    setRelations(rows);
    setSelectedRelationIds([]);
  }, [kbId, relationFilter]);

  const applyStatus = async (status: 'pending' | 'confirmed' | 'rejected') => {
    if (!kbId || selectedRelationIds.length === 0) {
      message.warning('请先选择要处理的关系');
      return;
    }
    setUpdatingRelations(true);
    try {
      const data = await setRelationsStatus({
        kb_id: kbId,
        relation_ids: selectedRelationIds,
        status,
      });
      const label = status === 'confirmed' ? '确认' : status === 'rejected' ? '拒绝' : '置为待确认';
      message.success(
        `已${label} ${data.updated} 条关系${status === 'confirmed' ? `，新增图谱边 ${data.kg_edges}` : ''}`,
      );
      await loadRelations();
      refetchStats();
    } catch (error) {
      message.error(`操作失败：${(error as Error).message}`);
    } finally {
      setUpdatingRelations(false);
    }
  };

  // 切换知识库或状态筛选时自动加载关系列表
  // （必须放在 loadRelations 定义之后，否则依赖数组求值会撞上 TDZ）
  useEffect(() => {
    if (kbId) void loadRelations();
  }, [kbId, loadRelations]);

  const erTables: ErTable[] = useMemo(
    () =>
      tables.map((table) => ({
        name: table.table,
        fields: table.fields.map((f) => f.name),
        primaryKey: primaryKeys[table.table],
      })),
    [tables, primaryKeys],
  );

  const erRelations: ErRelation[] = useMemo(() => {
    const fromJunctions: ErRelation[] = junctions.map((junction) => ({
      from: junction.left_table,
      to: junction.right_table,
      fromField: junction.left_field,
      toField: junction.right_field,
      label: junction.relation_type,
      cardinality: 'M:N',
    }));
    const fromFks: ErRelation[] = foreignKeys
      .filter((fk) => !junctions.some((j) => j.table === fk.child_table))
      .map((fk) => ({
        from: fk.child_table,
        to: fk.parent_table,
        fromField: fk.child_field,
        toField: fk.parent_field,
        label: '引用',
        cardinality: fk.cardinality,
      }));
    return [...fromJunctions, ...fromFks];
  }, [junctions, foreignKeys]);

  const tableColumns: ColumnsType<RelationalTable> = [
    { title: '表', dataIndex: 'table', width: 140 },
    { title: '文件', dataIndex: 'file', ellipsis: true },
    { title: '行数', dataIndex: 'row_count', width: 80 },
    {
      title: '主键',
      width: 200,
      render: (_, record) => (
        <Select
          size="small"
          style={{ width: 180 }}
          placeholder="选择主键"
          value={primaryKeys[record.table]}
          onChange={(value) =>
            setPrimaryKeys((prev) => ({ ...prev, [record.table]: value }))
          }
          options={record.fields.map((field) => ({
            label: `${field.name}（唯一 ${(field.unique_ratio * 100).toFixed(0)}%）`,
            value: field.name,
          }))}
        />
      ),
    },
    {
      title: '字段',
      render: (_, record) => (
        <Space wrap size={[4, 4]}>
          {record.fields.map((field) => (
            <Tag
              key={field.name}
              color={field.is_unique ? 'gold' : 'default'}
              title={`类型 ${field.type}｜空值 ${(field.null_ratio * 100).toFixed(0)}%`}
            >
              {field.name}
            </Tag>
          ))}
        </Space>
      ),
    },
  ];

  return (
    <AppLayout headerRight={<AdminHeaderRight />}>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div>
          <Typography.Title level={4} style={{ marginBottom: 4 }}>
            关联数据导入
          </Typography.Title>
          <Typography.Text type="secondary">
            自动识别主键、外键与关联表（1:1 / 1:N / M:N），低置信度候选默认
            <Tag color="orange" style={{ margin: '0 4px' }}>
              待确认
            </Tag>
            ，未经确认不会写入知识图谱。
          </Typography.Text>
        </div>

        <Card title="1. 选择结构化文件">
          <FilePicker onChange={setPicked} accept=".csv,.tsv,.json,.jsonl,.xlsx,.parquet" />
        </Card>

        <Card title="2. Schema 与关系分析">
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space wrap>
              <Select
                style={{ width: 260 }}
                placeholder="选择目标知识库"
                value={kbId}
                onChange={setKbId}
                options={kbs.map((kb) => ({ label: kb.name, value: kb.id }))}
              />
              <Button
                type="primary"
                icon={<SearchOutlined />}
                loading={analyzing}
                disabled={picked.length === 0}
                onClick={runAnalyze}
              >
                分析结构
              </Button>
              {progress > 0 && progress < 100 && (
                <Progress percent={progress} style={{ width: 200 }} />
              )}
            </Space>

            {analyzeErrors.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="部分文件无法分析"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {analyzeErrors.slice(0, 10).map((err, index) => (
                      <li key={index}>{err}</li>
                    ))}
                  </ul>
                }
              />
            )}

            {tables.length === 0 ? (
              <Empty description="分析后在此查看表结构、主键候选与关系候选" />
            ) : (
              <>
                <Table
                  rowKey="table"
                  size="small"
                  columns={tableColumns}
                  dataSource={tables}
                  pagination={false}
                  scroll={{ x: 900 }}
                />

                <Divider orientation="left" plain>
                  ER 图
                </Divider>
                <ErDiagram tables={erTables} relations={erRelations} />

                <Divider orientation="left" plain>
                  关联表候选（多对多）
                </Divider>
                {junctions.length === 0 ? (
                  <Empty description="未发现关联表" />
                ) : (
                  junctions.map((junction, index) => (
                    <Space key={junction.table} wrap style={{ marginBottom: 8 }}>
                      <Checkbox
                        checked={selectedJunctions.includes(index)}
                        onChange={(e) =>
                          setSelectedJunctions((prev) =>
                            e.target.checked
                              ? [...prev, index]
                              : prev.filter((i) => i !== index),
                          )
                        }
                      >
                        {junction.label}
                      </Checkbox>
                      <Tag color={STATUS_COLOR[junction.status]}>
                        {STATUS_LABEL[junction.status]}
                      </Tag>
                      <Tag color="blue">置信度 {(junction.confidence * 100).toFixed(0)}%</Tag>
                      <Input
                        size="small"
                        style={{ width: 140 }}
                        placeholder="关系类型"
                        defaultValue={junction.relation_type}
                        onChange={(e) =>
                          setRelationTypes((prev) => ({
                            ...prev,
                            [`jn-${index}`]: e.target.value,
                          }))
                        }
                      />
                      <Typography.Text type="secondary">{junction.evidence}</Typography.Text>
                    </Space>
                  ))
                )}

                <Divider orientation="left" plain>
                  外键候选
                </Divider>
                {foreignKeys.length === 0 ? (
                  <Empty description="未发现外键候选" />
                ) : (
                  <Table
                    size="small"
                    rowKey={(record) => `${record.child_table}.${record.child_field}`}
                    dataSource={foreignKeys}
                    pagination={false}
                    rowSelection={{
                      selectedRowKeys: selectedFks.map((i) => `${foreignKeys[i].child_table}.${foreignKeys[i].child_field}`),
                      onChange: (_keys, rows) =>
                        setSelectedFks(
                          rows.map(
                            (row) =>
                              foreignKeys.findIndex(
                                (fk) =>
                                  fk.child_table === row.child_table &&
                                  fk.child_field === row.child_field,
                              ),
                          ),
                        ),
                    }}
                    columns={[
                      {
                        title: '子表字段',
                        render: (_, record) => `${record.child_table}.${record.child_field}`,
                      },
                      {
                        title: '父表字段',
                        render: (_, record) => `${record.parent_table}.${record.parent_field}`,
                      },
                      { title: '基数', dataIndex: 'cardinality', width: 80 },
                      {
                        title: '置信度',
                        dataIndex: 'confidence',
                        width: 100,
                        render: (value: number) => `${(value * 100).toFixed(0)}%`,
                      },
                      { title: '依据', dataIndex: 'evidence', ellipsis: true },
                    ]}
                  />
                )}
              </>
            )}
          </Space>
        </Card>

        <Card title="3. 确认并导入">
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space wrap>
              <Checkbox
                checked={autoConfirm}
                onChange={(e) => setAutoConfirm(e.target.checked)}
              >
                导入时直接确认并写入知识图谱
              </Checkbox>
              <Button
                type="primary"
                icon={<ImportOutlined />}
                loading={committing}
                disabled={!kbId || tables.length === 0}
                onClick={runCommit}
              >
                开始导入
              </Button>
            </Space>
            {!autoConfirm && (
              <Alert
                type="info"
                showIcon
                message="未勾选直接确认时，关系将以「待确认」状态保存，不会进入知识图谱；可在导入后逐条确认。"
              />
            )}

            {commitResult && (
              <Space size="large" wrap>
                <Statistic title="原始记录" value={commitResult.records} />
                <Statistic title="实体" value={commitResult.entities} />
                <Statistic title="关系" value={commitResult.relations} />
                <Statistic title="图谱节点" value={commitResult.kg_nodes} />
                <Statistic title="图谱边" value={commitResult.kg_edges} />
                <Statistic
                  title="失败行"
                  value={commitResult.failed_rows}
                  valueStyle={{
                    color: commitResult.failed_rows > 0 ? '#cf1322' : '#3f8600',
                  }}
                />
              </Space>
            )}

            {commitResult?.errors?.length ? (
              <Descriptions
                bordered
                size="small"
                column={1}
                title="失败明细"
                items={commitResult.errors.slice(0, 20).map((item, index) => ({
                  key: String(index),
                  label: `${item.file_name}${item.row_no ? ` 第 ${item.row_no} 行` : ''}`,
                  children: item.message,
                }))}
              />
            ) : null}

            {stats && (
              <>
                <Divider orientation="left" plain>
                  知识库导入统计
                </Divider>
                <Space size="large" wrap>
                  <Statistic title="实体" value={stats.entities} />
                  <Statistic title="记录" value={stats.records} />
                  <Statistic title="关系总数" value={stats.relations_total} />
                  <Statistic
                    title="待确认"
                    value={stats.relations_pending}
                    valueStyle={{ color: '#fa8c16' }}
                  />
                  <Statistic
                    title="已确认"
                    value={stats.relations_confirmed}
                    valueStyle={{ color: '#3f8600' }}
                  />
                  <Statistic title="已拒绝" value={stats.relations_rejected} />
                </Space>
              </>
            )}
          </Space>
        </Card>

        <Card
          title="4. 关系确认与维护"
          extra={
            <Space>
              <Select
                size="small"
                style={{ width: 130 }}
                placeholder="状态筛选"
                value={relationFilter}
                onChange={setRelationFilter}
                options={[
                  { label: '全部', value: '' },
                  { label: '待确认', value: 'pending' },
                  { label: '已确认', value: 'confirmed' },
                  { label: '已拒绝', value: 'rejected' },
                ]}
              />
              <Button size="small" onClick={() => void loadRelations()}>
                刷新
              </Button>
            </Space>
          }
        >
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space wrap>
              <Button
                type="primary"
                disabled={selectedRelationIds.length === 0}
                loading={updatingRelations}
                onClick={() => applyStatus('confirmed')}
              >
                确认选中（写入图谱）
              </Button>
              <Button
                danger
                disabled={selectedRelationIds.length === 0}
                loading={updatingRelations}
                onClick={() => applyStatus('rejected')}
              >
                拒绝选中
              </Button>
              <Button
                disabled={selectedRelationIds.length === 0}
                loading={updatingRelations}
                onClick={() => applyStatus('pending')}
              >
                置为待确认
              </Button>
              <Typography.Text type="secondary">
                已选 {selectedRelationIds.length} / {relations.length} 条
              </Typography.Text>
            </Space>
            <Alert
              type="info"
              showIcon
              message="只有「已确认」的关系会参与知识图谱检索；拒绝或置为待确认会同步撤掉对应图谱边，避免出现无法溯源的关系。"
            />
            <Table
              size="small"
              rowKey="id"
              dataSource={relations}
              pagination={{ pageSize: 10, size: 'small' }}
              rowSelection={{
                selectedRowKeys: selectedRelationIds,
                onChange: (keys) => setSelectedRelationIds(keys as string[]),
              }}
              locale={{ emptyText: '当前知识库还没有关系记录' }}
              columns={[
                {
                  title: '源实体',
                  render: (_, row) => row.source?.name ?? '—',
                },
                { title: '关系', dataIndex: 'relation_type', width: 120 },
                {
                  title: '目标实体',
                  render: (_, row) => row.target?.name ?? '—',
                },
                {
                  title: '状态',
                  dataIndex: 'status',
                  width: 90,
                  render: (value: string) => (
                    <Tag color={STATUS_COLOR[value]}>{STATUS_LABEL[value] ?? value}</Tag>
                  ),
                },
                {
                  title: '置信度',
                  dataIndex: 'confidence',
                  width: 90,
                  render: (value: number) => `${(value * 100).toFixed(0)}%`,
                },
                {
                  title: '来源',
                  dataIndex: 'provenance',
                  ellipsis: true,
                  render: (value: string) => (
                    <Typography.Text type="secondary">{value}</Typography.Text>
                  ),
                },
              ]}
            />
          </Space>
        </Card>
      </Space>
    </AppLayout>
  );
}
