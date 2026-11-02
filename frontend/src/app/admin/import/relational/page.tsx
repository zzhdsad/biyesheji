'use client';

import { ImportOutlined, SearchOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Divider,
  Empty,
  Input,
  Modal,
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
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  matchImportTemplate,
  saveImportTemplate,
  setRelationsStatus,
  type ForeignKeyItem,
  type ImportTemplateMatchResult,
  type JunctionItem,
  type RelationRow,
  type RelationalTable,
} from '@/services/api';
import { describeApiError } from '@/utils/apiError';
import {
  buildTemplateConfig,
  canApplyTemplate,
  diffTemplate,
  tablesFieldMap,
  templateDependencyFields,
  templatePrimaryKeys,
  type TemplateFieldDiff,
} from '@/utils/importTemplate';

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

const TEMPLATE_STATUS: Record<
  string,
  { color: string; label: string; alert: 'success' | 'info' | 'warning' | 'error' }
> = {
  exact: { color: 'green', label: '完全匹配', alert: 'success' },
  compatible: { color: 'blue', label: '可用（Schema 有新增）', alert: 'warning' },
  mismatch: { color: 'red', label: '不兼容：缺少字段', alert: 'error' },
  incompatible: { color: 'red', label: '模板版本不可解释', alert: 'error' },
  none: { color: 'default', label: '未匹配到模板', alert: 'info' },
};

/** 稳定键：不随候选顺序变化，避免用数组下标标记选择导致错选。 */
function fkKey(item: ForeignKeyItem): string {
  return `${item.child_table}.${item.child_field}->${item.parent_table}.${item.parent_field}`;
}

function junctionKey(item: JunctionItem): string {
  return item.table;
}

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
  const [selectedFks, setSelectedFks] = useState<string[]>([]);
  const [selectedJunctions, setSelectedJunctions] = useState<string[]>([]);
  const [relationTypes, setRelationTypes] = useState<Record<string, string>>({});
  const [templateFingerprint, setTemplateFingerprint] = useState('');
  const [templateMatch, setTemplateMatch] = useState<ImportTemplateMatchResult | null>(null);
  const [matchingTemplate, setMatchingTemplate] = useState(false);
  const [appliedTemplateId, setAppliedTemplateId] = useState<string | null>(null);
  const [templateDetailsOpen, setTemplateDetailsOpen] = useState(false);
  const [saveTemplateOpen, setSaveTemplateOpen] = useState(false);
  const [saveTemplateName, setSaveTemplateName] = useState('');
  const [savingTemplate, setSavingTemplate] = useState(false);
  const templateMatchSeq = useRef(0);
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

  const runTemplateMatch = useCallback(
    async (targetKb: string | undefined, tableList: RelationalTable[]) => {
      if (tableList.length === 0) {
        setTemplateMatch(null);
        return;
      }
      const seq = templateMatchSeq.current + 1;
      templateMatchSeq.current = seq;
      // 重新匹配意味着当前记忆的“已套用模板”可能已过期：
      // 在结果返回前先撤销，防止旧 template_id 继续左右提交内容。
      setAppliedTemplateId(null);
      setMatchingTemplate(true);
      try {
        const result = await matchImportTemplate({
          kb_id: targetKb,
          tables: tablesFieldMap(tableList),
        });
        if (templateMatchSeq.current !== seq) return;
        setTemplateMatch(result);
      } catch (error) {
        if (templateMatchSeq.current !== seq) return;
        setTemplateMatch(null);
        message.warning(`模板匹配失败：${(error as Error).message}`);
      } finally {
        if (templateMatchSeq.current === seq) setMatchingTemplate(false);
      }
    },
    [],
  );

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
      setTemplateFingerprint(data.dataset_fingerprint);
      setTemplateMatch(null);
      setAppliedTemplateId(null);
      // 默认采用置信度最高的主键候选，用户可改
      const pks: Record<string, string> = {};
      Object.entries(data.primary_keys).forEach(([table, fields]) => {
        if (fields.length > 0) pks[table] = fields[0];
      });
      setPrimaryKeys(pks);
      setSelectedFks(data.foreign_keys.map(fkKey));
      setSelectedJunctions(data.junctions.map(junctionKey));
      setProgress(100);
      void runTemplateMatch(kbId, data.tables);
      const summary = `${data.tables.length} 张表，${data.foreign_keys.length} 个外键候选，${data.junctions.length} 个关联表`;
      if (data.tables.length === 0) {
        message.error(
          `分析失败：没有可分析的结构化文件${data.errors.length > 0 ? `（${data.errors[0]}）` : ''}`,
        );
      } else if (data.errors.length > 0) {
        message.warning(`分析完成，但 ${data.errors.length} 个文件无法分析：${summary}`);
      } else {
        message.success(`分析完成：${summary}`);
      }
    } catch (error) {
      message.error(`分析失败：${describeApiError(error)}`);
      setProgress(0);
    } finally {
      setAnalyzing(false);
    }
  };

  const templateDiff: TemplateFieldDiff | null = useMemo(
    () => (templateMatch?.config ? diffTemplate(templateMatch.config, tablesFieldMap(tables)) : null),
    [templateMatch, tables],
  );

  const templateApplicable = Boolean(
    templateMatch &&
      templateDiff &&
      templateMatch.template_id &&
      canApplyTemplate(templateMatch.status, templateDiff),
  );

  const markMappingEdited = () => {
    if (appliedTemplateId) setAppliedTemplateId(null);
  };

  const changeKb = (value: string | undefined) => {
    setKbId(value);
    setAppliedTemplateId(null);
    if (tables.length > 0) void runTemplateMatch(value, tables);
  };

  const applyTemplate = () => {
    if (!templateMatch?.config || !templateApplicable) {
      message.error('模板与当前数据不兼容，已阻止套用（请查看字段差异）');
      return;
    }
    const config = templateMatch.config;
    setPrimaryKeys((prev) => ({ ...prev, ...templatePrimaryKeys(config) }));
    setSelectedFks(
      foreignKeys
        .filter((fk) =>
          config.relations.some(
            (relation) =>
              relation.child_table === fk.child_table &&
              relation.child_field === fk.child_field &&
              relation.parent_table === fk.parent_table &&
              relation.parent_field === fk.parent_field,
          ),
        )
        .map(fkKey),
    );
    setSelectedJunctions(
      junctions
        .filter((junction) => config.junctions.some((item) => item.table === junction.table))
        .map(junctionKey),
    );
    const nextTypes: Record<string, string> = { ...relationTypes };
    for (const fk of foreignKeys) {
      const relation = config.relations.find(
        (item) =>
          item.child_table === fk.child_table &&
          item.child_field === fk.child_field &&
          item.parent_table === fk.parent_table &&
          item.parent_field === fk.parent_field,
      );
      if (relation) nextTypes[fkKey(fk)] = relation.relation_type;
    }
    for (const junction of junctions) {
      const item = config.junctions.find((j) => j.table === junction.table);
      if (item) nextTypes[junctionKey(junction)] = item.relation_type;
    }
    setRelationTypes(nextTypes);
    setAppliedTemplateId(templateMatch.template_id);
    message.success(`已套用模板「${templateMatch.template_name}」，可继续修改映射`);
  };

  const openSaveTemplate = () => {
    setSaveTemplateName(
      templateMatch?.template_name ? `${templateMatch.template_name}（副本）` : '',
    );
    setSaveTemplateOpen(true);
  };

  const saveAsTemplate = async () => {
    const name = saveTemplateName.trim();
    if (!name) {
      message.warning('请输入模板名称');
      return;
    }
    const selectedFkItems = foreignKeys.filter((fk) => selectedFks.includes(fkKey(fk)));
    const selectedJunctionItems = junctions.filter((j) =>
      selectedJunctions.includes(junctionKey(j)),
    );
    const config = buildTemplateConfig({
      tables: tables.map((table) => table.table),
      primaryKeys,
      foreignKeys: selectedFkItems.map((fk) => ({
        ...fk,
        relation_type: relationTypes[fkKey(fk)] ?? 'related_to',
      })),
      junctions: selectedJunctionItems.map((junction) => ({
        ...junction,
        relation_type:
          relationTypes[junctionKey(junction)] ?? junction.relation_type ?? 'related_to',
      })),
    });
    setSavingTemplate(true);
    try {
      const data = await saveImportTemplate({
        kb_id: kbId,
        name,
        fingerprint: templateFingerprint || undefined,
        tables: tablesFieldMap(tables),
        config,
      });
      if (data.error) {
        message.error(data.error);
        return;
      }
      message.success(`模板「${data.name}」已保存`);
      setSaveTemplateOpen(false);
      await runTemplateMatch(kbId, tables);
    } catch (error) {
      message.error(`保存模板失败：${(error as Error).message}`);
    } finally {
      setSavingTemplate(false);
    }
  };

  const runCommit = async () => {
    if (!kbId) {
      message.warning('请选择目标知识库');
      return;
    }
    setCommitting(true);
    try {
      const selectedFkItems = foreignKeys.filter((fk) => selectedFks.includes(fkKey(fk)));
      const selectedJunctionItems = junctions.filter((j) =>
        selectedJunctions.includes(junctionKey(j)),
      );
      const data = await commitRelationalImport({
        session_id: sessionId,
        kb_id: kbId,
        primary_keys: primaryKeys,
        foreign_keys: selectedFkItems.map((fk) => ({
          child_table: fk.child_table,
          child_field: fk.child_field,
          parent_table: fk.parent_table,
          parent_field: fk.parent_field,
          relation_type: relationTypes[fkKey(fk)] ?? 'related_to',
        })),
        junctions: selectedJunctionItems.map((junction) => ({
          table: junction.table,
          left_table: junction.left_table,
          left_field: junction.left_field,
          right_table: junction.right_table,
          right_field: junction.right_field,
          relation_type:
            relationTypes[junctionKey(junction)] ?? junction.relation_type,
        })),
        auto_confirm: autoConfirm,
        // 仅当模板未被修改时才随请求发送：改过映射就按当前选择导入，
        // 避免后端再用模板补齐用户已经取消的关系。
        template_id: appliedTemplateId ?? undefined,
      });
      setCommitResult(data);
      refetchStats();
      // 必须同时刷新关系列表：导入后关系状态（pending/confirmed）已变化，
      // 只刷新统计会让「关系确认」区仍显示空表（浏览器验收时实测到）。
      await loadRelations();
      const summary = `实体 ${data.entities}、关系 ${data.relations}、图谱边 ${data.kg_edges}`;
      if (data.failed_rows > 0 && data.entities === 0 && data.records === 0) {
        message.error(`导入失败：${summary}，失败行 ${data.failed_rows}`);
      } else if (data.failed_rows > 0) {
        message.warning(`导入完成但存在失败：${summary}，失败行 ${data.failed_rows}`);
      } else {
        message.success(`导入完成：${summary}`);
      }
    } catch (error) {
      message.error(`导入失败：${describeApiError(error)}`);
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
        <span data-testid={`pk-select-${record.table}`}>
          <Select
            size="small"
            style={{ width: 180 }}
            placeholder="选择主键"
            value={primaryKeys[record.table]}
            onChange={(value) => {
              markMappingEdited();
              setPrimaryKeys((prev) => ({ ...prev, [record.table]: value }));
            }}
            options={record.fields.map((field) => ({
              label: `${field.name}（唯一 ${(field.unique_ratio * 100).toFixed(0)}%）`,
              value: field.name,
            }))}
          />
        </span>
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

        <Card title="2. Schema 与关系分析" data-testid="schema-card">
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Space wrap>
              <span data-testid="kb-select">
                <Select
                  style={{ width: 260 }}
                  placeholder="选择目标知识库"
                  value={kbId}
                  onChange={changeKb}
                  options={kbs.map((kb) => ({ label: kb.name, value: kb.id }))}
                />
              </span>
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
                  junctions.map((junction) => (
                    <Space key={junction.table} wrap style={{ marginBottom: 8 }}>
                      <Checkbox
                        checked={selectedJunctions.includes(junctionKey(junction))}
                        onChange={(e) => {
                          markMappingEdited();
                          const key = junctionKey(junction);
                          setSelectedJunctions((prev) =>
                            e.target.checked
                              ? [...prev, key]
                              : prev.filter((item) => item !== key),
                          );
                        }}
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
                        onChange={(e) => {
                          markMappingEdited();
                          setRelationTypes((prev) => ({
                            ...prev,
                            [junctionKey(junction)]: e.target.value,
                          }));
                        }}
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
                    rowKey={fkKey}
                    dataSource={foreignKeys}
                    pagination={false}
                    rowSelection={{
                      selectedRowKeys: selectedFks,
                      onChange: (keys) => {
                        markMappingEdited();
                        setSelectedFks(keys.map(String));
                      },
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

        <Card
          title="3. 映射模板"
          data-testid="template-card"
          extra={
            <Space>
              <Button
                size="small"
                loading={matchingTemplate}
                disabled={tables.length === 0}
                onClick={() => void runTemplateMatch(kbId, tables)}
              >
                重新匹配
              </Button>
              <Button
                size="small"
                disabled={tables.length === 0}
                onClick={openSaveTemplate}
                data-testid="save-template"
              >
                另存为新模板
              </Button>
            </Space>
          }
        >
          {tables.length === 0 ? (
            <Empty description="完成结构分析后，系统会自动匹配可复用的映射模板" />
          ) : (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Space wrap>
                <Tag
                  color={TEMPLATE_STATUS[templateMatch?.status ?? 'none'].color}
                  data-testid="template-status"
                >
                  {TEMPLATE_STATUS[templateMatch?.status ?? 'none'].label}
                </Tag>
                {templateMatch?.template_name && (
                  <Typography.Text strong>{templateMatch.template_name}</Typography.Text>
                )}
                <Typography.Text type="secondary">
                  数据集指纹 {templateFingerprint ? `${templateFingerprint.slice(0, 16)}…` : '—'}
                </Typography.Text>
                {appliedTemplateId && (
                  <Tag color="green" data-testid="template-applied">
                    已套用模板
                  </Tag>
                )}
                {templateMatch?.template_id && !appliedTemplateId && (
                  <Tag color="orange" data-testid="template-not-applied">
                    模板未套用（当前按手动映射）
                  </Tag>
                )}
              </Space>

              {templateMatch ? (
                <Alert
                  type={TEMPLATE_STATUS[templateMatch.status].alert}
                  showIcon
                  message={templateMatch.message}
                  description={
                    templateDiff &&
                    (Object.keys(templateDiff.missing_fields).length > 0 ||
                      Object.keys(templateDiff.extra_fields).length > 0) ? (
                      <Space direction="vertical" size={2}>
                        {Object.entries(templateDiff.missing_fields).map(([table, fields]) => (
                          <Typography.Text key={table} type="danger">
                            缺少：{table} → {fields.join('、')}
                          </Typography.Text>
                        ))}
                        {Object.entries(templateDiff.extra_fields).map(([table, fields]) => (
                          <Typography.Text key={table} type="secondary">
                            模板未覆盖：{table}（{fields.join('、') || '无字段'}）
                          </Typography.Text>
                        ))}
                      </Space>
                    ) : undefined
                  }
                />
              ) : (
                <Alert
                  type="info"
                  showIcon
                  message="未匹配到可复用的映射模板，将按当前自动识别的映射导入。"
                />
              )}

              <Space wrap>
                <Button
                  disabled={!templateMatch?.config}
                  onClick={() => setTemplateDetailsOpen(true)}
                  data-testid="template-details"
                >
                  查看映射详情
                </Button>
                <Button
                  type="primary"
                  disabled={!templateApplicable}
                  onClick={applyTemplate}
                  data-testid="apply-template"
                >
                  套用模板
                </Button>
                {!templateApplicable && templateMatch?.status !== 'none' && (
                  <Typography.Text type="danger">
                    模板不兼容，已禁止套用（避免静默使用错误映射）
                  </Typography.Text>
                )}
              </Space>

              {appliedTemplateId && (
                <Alert
                  type="success"
                  showIcon
                  message="已套用模板：主键与关系已按模板填充。修改任一映射后将不再随导入提交模板，按当前选择导入。"
                />
              )}
              {!appliedTemplateId && templateMatch?.template_id && (
                <Typography.Text type="secondary">
                  修改主键或勾选关系后，导入只使用当前显式映射；需要模板兜底时请重新套用。
                </Typography.Text>
              )}
            </Space>
          )}
        </Card>

        <Card title="4. 确认并导入">
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
                data-testid="commit-button"
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
          title="5. 关系确认与维护"
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

        <Modal
          open={templateDetailsOpen}
          title="映射模板详情"
          width={900}
          footer={<Button onClick={() => setTemplateDetailsOpen(false)}>关闭</Button>}
          onCancel={() => setTemplateDetailsOpen(false)}
        >
          {templateMatch?.config ? (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Descriptions
                size="small"
                bordered
                column={2}
                items={[
                  { key: 'name', label: '模板', children: templateMatch.template_name ?? '—' },
                  {
                    key: 'status',
                    label: '匹配状态',
                    children: TEMPLATE_STATUS[templateMatch.status].label,
                  },
                  {
                    key: 'fingerprint',
                    label: '指纹',
                    children: templateFingerprint || '—',
                  },
                  {
                    key: 'scope',
                    label: '作用范围',
                    children: templateMatch.template_id
                      ? kbId
                        ? '当前知识库（含全局共享模板）'
                        : '全局共享模板'
                      : '—',
                  },
                ]}
              />
              <Divider orientation="left" plain>
                表映射
              </Divider>
              <Table
                size="small"
                rowKey="table"
                pagination={false}
                dataSource={Object.values(templateMatch.config.tables)}
                columns={[
                  { title: '表', dataIndex: 'table' },
                  { title: '实体类型', dataIndex: 'entity_type' },
                  {
                    title: '主键',
                    dataIndex: 'pk_field',
                    render: (value: string | null) => value ?? '—',
                  },
                  {
                    title: '显示字段',
                    dataIndex: 'display_field',
                    render: (value: string | null) => value ?? '—',
                  },
                ]}
              />
              <Divider orientation="left" plain>
                关系映射
              </Divider>
              <Table
                size="small"
                rowKey={(row) =>
                  `${row.child_table}.${row.child_field}->${row.parent_table}.${row.parent_field}`
                }
                pagination={false}
                dataSource={templateMatch.config.relations}
                locale={{ emptyText: '模板未声明外键关系' }}
                columns={[
                  {
                    title: '子表字段',
                    render: (_, row) => `${row.child_table}.${row.child_field}`,
                  },
                  {
                    title: '父表字段',
                    render: (_, row) => `${row.parent_table}.${row.parent_field}`,
                  },
                  { title: '关系类型', dataIndex: 'relation_type', width: 140 },
                ]}
              />
              <Divider orientation="left" plain>
                关联表映射
              </Divider>
              <Table
                size="small"
                rowKey="table"
                pagination={false}
                dataSource={templateMatch.config.junctions}
                locale={{ emptyText: '模板未声明关联表' }}
                columns={[
                  { title: '关联表', dataIndex: 'table' },
                  {
                    title: '左表字段',
                    render: (_, row) => `${row.left_table}.${row.left_field}`,
                  },
                  {
                    title: '右表字段',
                    render: (_, row) => `${row.right_table}.${row.right_field}`,
                  },
                  { title: '关系类型', dataIndex: 'relation_type', width: 140 },
                ]}
              />
              <Divider orientation="left" plain>
                模板依赖字段
              </Divider>
              <Table
                size="small"
                rowKey={(row) => `${row.table}.${row.field}.${row.kind}`}
                pagination={false}
                dataSource={templateDependencyFields(templateMatch.config)}
                columns={[
                  { title: '表', dataIndex: 'table' },
                  { title: '字段', dataIndex: 'field' },
                  { title: '用途', dataIndex: 'kind', width: 120 },
                ]}
              />
            </Space>
          ) : (
            <Empty description="当前没有可展示的模板配置" />
          )}
        </Modal>

        <Modal
          open={saveTemplateOpen}
          title="另存为新模板"
          okText="保存"
          confirmLoading={savingTemplate}
          onOk={() => void saveAsTemplate()}
          onCancel={() => setSaveTemplateOpen(false)}
        >
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Typography.Text type="secondary">
              模板将保存到{kbId ? '当前知识库（仅该库及管理员可见）' : '全局共享范围'}，
              保存的是「当前」主键与关系选择；下次导入相同数据集时自动匹配。
            </Typography.Text>
            <Input
              placeholder="模板名称，例如：D1-D7 标准映射"
              value={saveTemplateName}
              onChange={(e) => setSaveTemplateName(e.target.value)}
              onPressEnter={() => void saveAsTemplate()}
              maxLength={80}
            />
            <Typography.Text type="secondary">
              数据集指纹：{templateFingerprint || '（由服务端按当前字段计算）'}
            </Typography.Text>
            {Object.keys(templateDiff?.missing_fields ?? {}).length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="当前模板与数据集存在字段差异"
                description="另存会以当前手动映射为准；保存前请确认缺失字段对应的表已被正确配置。"
              />
            )}
          </Space>
        </Modal>
      </Space>
    </AppLayout>
  );
}
