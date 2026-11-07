'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';
import {
  ArrowLeftOutlined,
  CloudSyncOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { RcFile, UploadFile } from 'antd/es/upload';
import type {
  DocumentItem,
  GroupProcessingInfo,
  StructuredField,
  StructuredGroup,
  StructuredGroupDetail,
  StructuredGroupFile,
  StructuredGroupRecordDetail,
  StructuredGroupRecordItem,
  StructuredGroupValidation,
  StructuredRelation,
} from '@/types';
import {
  addStructuredGroupFiles,
  confirmStructuredGroup,
  createStructuredGroup,
  deleteStructuredGroup,
  fetchDocumentsPage,
  fetchGroupProcessing,
  fetchStructuredGroup,
  fetchStructuredGroupRecord,
  fetchStructuredGroupRecords,
  fetchStructuredGroups,
  removeStructuredGroupFile,
  renameStructuredGroup,
  revectorizeStructuredGroup,
  saveStructuredRelations,
  setStructuredGroupFileAlias,
  setStructuredRelationConfirmation,
  suggestStructuredRelations,
  uploadDocument,
  validateStructuredGroup,
} from '@/services/api';
import { describeApiError } from '@/utils/apiError';
import { STRUCTURED_FORMATS } from '@/utils/documentFormats';
import {
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  cellValueText,
  fieldTypeLabel,
  structuredParseStatusMeta,
} from '@/utils/structuredDisplay';
import {
  groupRowStatus,
  memberParseStatus,
  vectorStatusMeta,
} from '@/utils/processingStatus';
import {
  ProcessingOverviewPanel,
  ProcessingStatusTag,
} from '@/components/common/ProcessingStatusTag';
import { useProcessingOverview } from '@/hooks/useProcessingOverview';

/** 关联数据只接受结构化格式（普通文档走第三阶段之外的解析链路，不参与文件组）。 */
const GROUP_ACCEPT = STRUCTURED_FORMATS.map((f) => `.${f}`).join(',');
const GROUP_ACCEPT_HINT = STRUCTURED_FORMATS.map((f) => f.toUpperCase()).join(' / ');

/** 文件组自己的业务状态文案（统一状态之外的细粒度说明，仍保留原语义）。 */
const GROUP_STATUS_MAP: Record<string, string> = {
  draft: '待配置/待校验',
  validated: '已校验，未确认',
  confirmed: '已生成关联记录',
  stale: '需重新校验',
};

/** 与后端 RELATION_KIND_LABELS 一致的中文展示。 */
const KIND_LABEL: Record<string, string> = {
  one_to_one: '一对一',
  one_to_many: '一对多',
  many_to_one: '多对一',
  many_to_many: '多对多',
};
const KIND_OPTIONS = Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }));

/** 推测评分：0–100 规则打分（不是概率），只用于分级展示与自动预填判断。 */
function relationScoreTag(score?: number | null, tier?: string | null) {
  if (score == null) return <Typography.Text type="secondary">-</Typography.Text>;
  const meta =
    tier === 'high'
      ? { color: 'success', label: '高' }
      : tier === 'medium'
        ? { color: 'warning', label: '中' }
        : { color: 'default', label: '低' };
  return (
    <Space size={4}>
      <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
        {score} 分
      </Tag>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {meta.label}
      </Typography.Text>
    </Space>
  );
}

/** 后端 relation_stats 的单行结构（接口里是自由字典，这里按实际字段收窄便于展示）。 */
interface RelationStatRow {
  relation_id: string;
  from_file: string;
  from_field: string;
  to_file: string;
  to_field: string;
  kind: string;
  confirmed: boolean;
  left_rows: number;
  right_rows: number;
  matched: number;
  unmatched: number;
  empty_key: number;
  left_duplicate_keys: number;
  left_duplicate_rows: number;
  duplicate_keys: number;
  duplicate_key_rows: number;
  orphan_right_rows: number;
}

/** 文件组状态：统一标签 + 业务细粒度文案（悬浮说明里可见）。 */
function groupStatusTag(group: StructuredGroup) {
  return (
    <ProcessingStatusTag
      status={groupRowStatus(group)}
      detail={GROUP_STATUS_MAP[group.status] ?? group.status}
    />
  );
}

/** 组内成员文件的解析状态：与结构化列表用同一套词汇，避免三处三种叫法。 */
function parseStatusTag(status?: string | null, recordCount?: number | null, error?: string) {
  return (
    <ProcessingStatusTag
      status={memberParseStatus(status, recordCount)}
      detail={structuredParseStatusMeta(status).label}
      error={error}
    />
  );
}

/** 文件在组里的展示名：角色名优先，其次当前文件名，最后建组时快照。 */
function fileLabel(file: StructuredGroupFile | undefined, docId?: string): string {
  if (!file || file.doc_id !== docId) return docId ?? '';
  return file.alias || file.current_file_name || file.file_name;
}

interface Props {
  kbId: string | null;
  /** 新建文件组时上传文件落在哪个文件夹（与结构化列表当前目录一致）。 */
  folderId?: string | null;
  open: boolean;
  /** 工具栏「上传关联数据」进入时直接打开上传弹窗。 */
  autoCreate?: boolean;
  /** 引用来源跳转：打开时直接进入指定文件组详情。 */
  initialGroupId?: string | null;
  onClose: () => void;
  /** 组内文件上传/移出后通知文档列表刷新。 */
  onChanged?: () => void;
}

export function StructuredGroupsDrawer({
  kbId,
  folderId,
  open,
  autoCreate,
  initialGroupId,
  onClose,
  onChanged,
}: Props) {
  const [groups, setGroups] = useState<StructuredGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<StructuredGroup | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const autoCreateHandled = useRef(false);

  const loadGroups = useCallback(async () => {
    if (!kbId) {
      setGroups([]);
      return;
    }
    setLoading(true);
    try {
      setGroups(await fetchStructuredGroups(kbId));
      setError(null);
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    if (!open) {
      setDetailId(null);
      setGroups([]);
      setError(null);
      setUploadOpen(false);
      autoCreateHandled.current = false;
      return;
    }
    void loadGroups();
  }, [open, loadGroups]);

  useEffect(() => {
    if (!autoCreate) {
      autoCreateHandled.current = false;
      return;
    }
    if (!open || !kbId || autoCreateHandled.current) return;
    autoCreateHandled.current = true;
    setUploadOpen(true);
  }, [open, autoCreate, kbId]);

  // 引用来源跳转：带 group_id 打开时直接进入该组详情（只在打开时生效一次）
  const initialGroupHandled = useRef<string | null>(null);
  useEffect(() => {
    if (!open || !initialGroupId) return;
    if (initialGroupHandled.current === initialGroupId) return;
    initialGroupHandled.current = initialGroupId;
    setDetailId(initialGroupId);
  }, [open, initialGroupId]);

  const handleDelete = async (group: StructuredGroup) => {
    try {
      await deleteStructuredGroup(group.id);
      message.success(`文件组「${group.name}」已删除（只删关联配置与关联记录，不删原始文件）`);
      void loadGroups();
    } catch (err) {
      message.error(describeApiError(err));
    }
  };

  const handleRename = async () => {
    if (!renameTarget) return;
    const next = renameValue.trim();
    if (!next) {
      message.warning('请填写文件组名称');
      return;
    }
    try {
      await renameStructuredGroup(renameTarget.id, next);
      message.success('已重命名');
      setRenameTarget(null);
      void loadGroups();
    } catch (err) {
      message.error(describeApiError(err));
    }
  };

  const columns: ColumnsType<StructuredGroup> = [
    {
      title: '文件组',
      dataIndex: 'name',
      render: (name: string, row) => (
        <Space direction="vertical" size={0}>
          <a onClick={() => setDetailId(row.id)}>{name}</a>
          {row.error_message && (
            <Typography.Text type="warning" style={{ fontSize: 12 }}>
              {row.error_message}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 150,
      render: (_: string, row) => groupStatusTag(row),
    },
    { title: '文件', dataIndex: 'file_count', width: 70 },
    {
      title: '关系',
      key: 'relations',
      width: 130,
      render: (_, row) => (
        <Space size={4}>
          <span>{row.relation_count} 条</span>
          {row.unconfirmed_count > 0 && (
            <Tooltip title="推测出来的关系需要你逐条确认，未确认的不参与生成">
              <Tag color="orange">{row.unconfirmed_count} 条待确认</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '关联记录',
      dataIndex: 'record_count',
      width: 100,
      render: (n: number) => (n > 0 ? `${n} 条` : '-'),
    },
    {
      title: '向量索引',
      dataIndex: 'vector_status',
      width: 140,
      render: (_: string, row) => {
        if (!row.record_count) return '-';
        const meta = vectorStatusMeta(row.vector_status);
        return (
          <Tooltip title={row.vector_error || undefined}>
            <Tag color={meta.color} style={{ margin: 0 }}>
              {meta.label}
              {row.vector_count > 0 ? ` · ${row.vector_count}` : ''}
            </Tag>
          </Tooltip>
        );
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 190,
      render: (_, row) => (
        <Space size={4}>
          <Button size="small" type="link" onClick={() => setDetailId(row.id)}>
            配置
          </Button>
          <Button
            size="small"
            type="link"
            onClick={() => {
              setRenameTarget(row);
              setRenameValue(row.name);
            }}
          >
            重命名
          </Button>
          <Popconfirm
            title="删除文件组？"
            description="只删除关联配置与生成的关联记录，组内的原始文件保留。"
            okText="删除"
            okButtonProps={{ danger: true }}
            onConfirm={() => void handleDelete(row)}
          >
            <Button size="small" type="link" danger>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Drawer
      title={detailId ? '关联数据文件组' : '关联数据管理'}
      width={detailId ? 1120 : 860}
      open={open}
      onClose={onClose}
      destroyOnClose
    >
      {detailId ? (
        <GroupDetailPanel
          groupId={detailId}
          onBack={() => {
            setDetailId(null);
            void loadGroups();
          }}
          onChanged={onChanged}
        />
      ) : (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Space wrap>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              disabled={!kbId}
              onClick={() => setUploadOpen(true)}
            >
              上传关联数据（新文件组）
            </Button>
            <Button icon={<ReloadOutlined />} onClick={() => void loadGroups()}>
              刷新
            </Button>
          </Space>
          {!kbId && <Alert type="warning" showIcon message="请先在页面上选择知识库。" />}
          <Alert
            type="info"
            showIcon
            message="一个文件组 = 一个关联导入任务"
            description={
              <>
                关联数据（如 学生表 + 课程表 + 选课表）必须整组上传、整组配置关系，
                系统不会把普通的多文件批量上传当作关联数据导入；
                单个文件的字段识别与记录浏览仍走「结构化数据预览」。
              </>
            }
          />
          {error && <Alert type="error" showIcon message={error} />}
          <Table
            rowKey="id"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={groups}
            pagination={false}
            locale={{ emptyText: <Empty description="还没有关联数据文件组" /> }}
          />
        </Space>
      )}

      <GroupUploadModal
        kbId={kbId}
        folderId={folderId}
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        onCreated={(id) => {
          setUploadOpen(false);
          void loadGroups();
          onChanged?.();
          setDetailId(id);
        }}
      />

      <Modal
        title="重命名文件组"
        open={renameTarget !== null}
        onCancel={() => setRenameTarget(null)}
        onOk={() => void handleRename()}
        okText="保存"
      >
        <Input
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          placeholder="如「学生-课程-选课」"
        />
      </Modal>
    </Drawer>
  );
}

/** 关联数据上传：一次多选 / 整个文件夹，全部成功后才建组，避免半套关联。 */
function GroupUploadModal({
  kbId,
  folderId,
  open,
  onClose,
  onCreated,
}: {
  kbId: string | null;
  folderId?: string | null;
  open: boolean;
  onClose: () => void;
  onCreated: (groupId: string) => void;
}) {
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [name, setName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [progress, setProgress] = useState('');

  useEffect(() => {
    if (!open) {
      setFileList([]);
      setName('');
      setProgress('');
      setSubmitting(false);
    }
  }, [open]);

  const handleCreate = async () => {
    const files = fileList
      .map((f) => f.originFileObj as RcFile | undefined)
      .filter((f): f is RcFile => Boolean(f));
    if (!kbId) {
      message.warning('请先选择知识库');
      return;
    }
    if (files.length < 2) {
      message.warning('关联数据至少需要 2 个文件（例如学生表 + 课程表 + 选课表）');
      return;
    }
    setSubmitting(true);
    const ids: string[] = [];
    const failed: string[] = [];
    for (let i = 0; i < files.length; i += 1) {
      const file = files[i];
      setProgress(`上传中 ${i + 1}/${files.length}：${file.name}`);
      try {
        const doc = await uploadDocument(file, kbId, undefined, {
          space: 'structured',
          folderId,
        });
        ids.push(doc.id);
      } catch (err) {
        failed.push(`${file.name}（${describeApiError(err)}）`);
      }
    }
    // 只要有文件没传上去就不建组：宁可整组重来，也不要一个缺成员的关联数据
    if (failed.length) {
      setProgress('');
      setSubmitting(false);
      Modal.error({
        title: '本组文件未上传完整，已取消创建文件组',
        width: 620,
        content: (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Typography.Text>
              成功 {ids.length} 个、失败 {failed.length} 个。已上传的文件仍保留在结构化数据列表中，
              但不会组成关联文件组，也不会生成任何关联记录。请补齐后重新提交。
            </Typography.Text>
            {failed.map((f) => (
              <Typography.Text key={f} type="danger" style={{ fontSize: 12 }}>
                {f}
              </Typography.Text>
            ))}
          </Space>
        ),
      });
      return;
    }
    setProgress('建组中…');
    try {
      const group = await createStructuredGroup({
        kb_id: kbId,
        name: name.trim() || files.map((f) => f.name).slice(0, 2).join(' + '),
        doc_ids: ids,
      });
      message.success(`文件组「${group.name}」已创建，共 ${ids.length} 个文件`);
      onCreated(group.id);
    } catch (err) {
      message.error(`文件已上传，但创建文件组失败：${describeApiError(err)}`);
    } finally {
      setProgress('');
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="上传关联数据（一个文件组）"
      open={open}
      width={640}
      onCancel={() => (submitting ? undefined : onClose())}
      maskClosable={false}
      okText="上传并创建文件组"
      okButtonProps={{ loading: submitting, disabled: !kbId || fileList.length < 2 }}
      onOk={() => void handleCreate()}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message={`一次选择的所有文件属于同一个关联数据文件组，仅接受 ${GROUP_ACCEPT_HINT}。`}
          description="文件组创建后状态是「待配置」：先确认字段与关系，再由你确认后才会生成关联知识记录。"
        />
        <Input
          addonBefore="文件组名称"
          value={name}
          maxLength={80}
          onChange={(e) => setName(e.target.value)}
          placeholder="如「学生-课程-选课」「中药饮片-药性-饮片关联」"
        />
        <Space wrap>
          <Upload
            multiple
            accept={GROUP_ACCEPT}
            fileList={fileList}
            beforeUpload={() => false}
            onChange={({ fileList: fl }) => setFileList(fl)}
          >
            <Button icon={<PlusOutlined />}>选择文件（可多选）</Button>
          </Upload>
          <Upload
            directory
            accept={GROUP_ACCEPT}
            fileList={fileList}
            beforeUpload={() => false}
            onChange={({ fileList: fl }) => setFileList(fl)}
          >
            <Button icon={<PlusOutlined />}>选择文件夹</Button>
          </Upload>
        </Space>
        <Typography.Text type="secondary">
          已选 {fileList.length} 个文件{fileList.length < 2 ? '（至少 2 个）' : ''}
          {progress ? ` · ${progress}` : ''}
        </Typography.Text>
      </Space>
    </Modal>
  );
}

/** 文件组详情：文件与结构 / 关系配置 / 校验报告 / 关联记录。 */
function GroupDetailPanel({
  groupId,
  onBack,
  onChanged,
}: {
  groupId: string;
  onBack: () => void;
  onChanged?: () => void;
}) {
  const [detail, setDetail] = useState<StructuredGroupDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<StructuredRelation[]>([]);
  const [adopted, setAdopted] = useState<Record<string, boolean>>({});
  const [candidateKinds, setCandidateKinds] = useState<Record<string, string>>({});
  const [manual, setManual] = useState({
    fromDoc: '',
    fromField: '',
    toDoc: '',
    toField: '',
    kind: 'many_to_one',
  });
  const [report, setReport] = useState<StructuredGroupValidation | null>(null);
  const [aliasTarget, setAliasTarget] = useState<StructuredGroupFile | null>(null);
  const [aliasValue, setAliasValue] = useState('');
  const [addFilesOpen, setAddFilesOpen] = useState(false);
  const [addFilesOptions, setAddFilesOptions] = useState<DocumentItem[]>([]);
  const [addFilesSelected, setAddFilesSelected] = useState<string[]>([]);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [anchorDocId, setAnchorDocId] = useState<string | undefined>(undefined);
  /** needForce = 需要勾选知情；force = 用户已勾选。二者分开，避免替用户默认勾选。 */
  const [needForce, setNeedForce] = useState(false);
  const [force, setForce] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  const loadDetail = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchStructuredGroup(groupId);
      setDetail(data);
      setError(null);
      // 后端未校验过的组返回 validation={}：不能当成完整校验报告（issues 会缺失）
      const validation = (data.validation ?? {}) as Partial<StructuredGroupValidation>;
      setReport(Array.isArray(validation.issues) ? (validation as StructuredGroupValidation) : null);
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      setLoading(false);
    }
  }, [groupId]);

  useEffect(() => {
    setCandidates([]);
    setAdopted({});
    setReport(null);
    void loadDetail();
  }, [groupId, loadDetail]);

  /**
   * 处理概览（第五阶段）：文件组「上传 → 字段识别 → 关系推测 → 用户确认 → 校验 → 生成 → 完成」
   * 的阶段、卡点与下一步全部由后端从库字段派生，前端不自行估算进度。
   * 组内还有文件在解析时后端给出「处理中」，本面板随之轮询；其余状态由用户操作驱动。
   */
  const processing = useProcessingOverview<GroupProcessingInfo>(
    () => fetchGroupProcessing(groupId),
    true,
    groupId,
    // 文件还在「已派发未开始 / 解析中」时也要继续轮询：默认只跟 processing，
    // 组刚建好时全部是 pending，会在文件解析完成前就停止刷新。
    (view) =>
      view.status === 'processing' ||
      view.members.some(
        (m) => !m.removed && (m.parse_status === 'pending' || m.parse_status === 'parsing'),
      ),
  );

  // detail 是校验 / 确认 / 关系增删 / 成员变动的唯一回执，随之重读一次概览即可反映最新阶段
  useEffect(() => {
    processing.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail]);

  const files = useMemo(() => detail?.files ?? [], [detail]);

  // 概览已确认没有文件在解析，但文件表仍是解析前的旧快照（“处理中”、记录数/字段数为 0）：
  // 补读一次详情即可拿到真实解析结果。这里只读状态，不重新解析、不重复派发任务。
  const detailSyncing = useRef(false);
  useEffect(() => {
    const members = processing.view?.members;
    if (!members?.length) return;
    const viewRunning = members.some(
      (m) => !m.removed && (m.parse_status === 'pending' || m.parse_status === 'parsing'),
    );
    if (viewRunning) return;
    const detailRunning = files.some(
      (f) => !f.removed && (f.parse_status === 'pending' || f.parse_status === 'parsing'),
    );
    if (!detailRunning || detailSyncing.current) return;
    detailSyncing.current = true;
    void loadDetail().finally(() => {
      detailSyncing.current = false;
    });
  }, [processing.view, files, loadDetail]);
  const fileById = useMemo(() => {
    const map = new Map<string, StructuredGroupFile>();
    files.forEach((f) => map.set(f.doc_id, f));
    return map;
  }, [files]);
  const label = useCallback(
    (docId?: string) => fileLabel(docId ? fileById.get(docId) : undefined, docId),
    [fileById],
  );
  const relationExists = useCallback(
    (id: string) => Boolean(detail?.relations.some((rel) => rel.id === id)),
    [detail],
  );

  const afterRelationsChanged = async () => {
    await loadDetail();
    setCandidates([]);
    setAdopted({});
  };

  const runSuggest = async () => {
    setBusy('suggest');
    try {
      const list = await suggestStructuredRelations(groupId);
      setCandidates(list);
      setAdopted({});
      setCandidateKinds(
        Object.fromEntries(list.map((r) => [r.id, String(r.kind ?? 'one_to_many')])),
      );
      // 高置信度结果已由后端预填为「未确认」关系，重读详情把它们显示出来
      await loadDetail();
      if (!list.length) {
        message.info('没有找到值能对上的候选关系，可以直接在下方手动选择文件和字段。');
        return;
      }
      const high = list.filter((r) => r.tier === 'high').length;
      const medium = list.filter((r) => r.tier === 'medium').length;
      const low = list.length - high - medium;
      const prefilled = list.filter((r) => r.prefilled).length;
      message.success(
        `推测出 ${list.length} 条候选（高 ${high} / 中 ${medium} / 低 ${low}）` +
          (prefilled ? `，已自动预填 ${prefilled} 条高置信度关系（未确认，请逐条确认）` : ''),
      );
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  const adoptCandidates = async () => {
    const chosen = candidates.filter((c) => adopted[c.id]);
    if (!chosen.length) {
      message.warning('请勾选确实成立的关系后再保存；仅字段名相似不构成关联依据。');
      return;
    }
    setBusy('adopt');
    try {
      const payload = [
        ...(detail?.relations ?? []).map(toRelationPayload),
        ...chosen.map((c) => ({
          from_doc_id: c.from_doc_id,
          from_field: c.from_field,
          to_doc_id: c.to_doc_id,
          to_field: c.to_field,
          kind: candidateKinds[c.id] ?? String(c.kind ?? 'one_to_many'),
          confirmed: true,
          origin: 'suggested',
          confidence: c.confidence ?? null,
          score: c.score ?? null,
          tier: c.tier ?? null,
          coverage: c.coverage ?? undefined,
          row_coverage: c.row_coverage ?? undefined,
          evidence: c.evidence ?? undefined,
          reason: c.reason ?? '推测结果，经用户勾选确认',
        })),
      ];
      await saveStructuredRelations(groupId, payload);
      message.success(`已保存 ${chosen.length} 条你确认的关系，请重新校验`);
      await afterRelationsChanged();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  const addManualRelation = async () => {
    if (!manual.fromDoc || !manual.toDoc || !manual.fromField || !manual.toField) {
      message.warning('请选择两侧的文件与关联字段');
      return;
    }
    if (manual.fromDoc === manual.toDoc) {
      message.warning('关联关系必须连接两个不同文件');
      return;
    }
    setBusy('manual');
    try {
      await saveStructuredRelations(groupId, [
        ...(detail?.relations ?? []).map(toRelationPayload),
        {
          from_doc_id: manual.fromDoc,
          from_field: manual.fromField,
          to_doc_id: manual.toDoc,
          to_field: manual.toField,
          kind: manual.kind,
          confirmed: true,
          origin: 'manual',
          confidence: null,
          reason: '用户手动配置',
        },
      ]);
      message.success('已添加关系，请重新校验');
      await afterRelationsChanged();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  const dropRelation = async (rel: StructuredRelation) => {
    setBusy(`drop-${rel.id}`);
    try {
      await saveStructuredRelations(
        groupId,
        (detail?.relations ?? []).filter((r) => r.id !== rel.id).map(toRelationPayload),
      );
      message.success('已删除该关系');
      await afterRelationsChanged();
    } catch (err) {
      message.error(describeApiError(err));
      // 后端可能已经提交但响应失败（或关系已被其他操作删除）：重读详情，
      // 避免继续用旧关系 ID 操作。
      await loadDetail();
    } finally {
      setBusy(null);
    }
  };

  const updateRelationKind = async (rel: StructuredRelation, kind: string) => {
    if (kind === rel.kind) return;
    setBusy(`kind-${rel.id}`);
    try {
      await saveStructuredRelations(
        groupId,
        (detail?.relations ?? []).map((item) =>
          item.id === rel.id
            ? { ...toRelationPayload(item), kind }
            : toRelationPayload(item),
        ),
      );
      message.success('已更新关联类型，请重新校验');
      await loadDetail();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  const toggleConfirm = async (rel: StructuredRelation, confirmed: boolean) => {
    setBusy(`confirm-${rel.id}`);
    try {
      await setStructuredRelationConfirmation(groupId, rel.id, confirmed);
      await loadDetail();
    } catch (err) {
      message.error(describeApiError(err));
      await loadDetail();
    } finally {
      setBusy(null);
    }
  };

  const runValidate = async () => {
    setBusy('validate');
    try {
      const data = await validateStructuredGroup(groupId);
      setReport(data);
      await loadDetail();
      message.success(
        data.error_count
          ? `校验完成：${data.error_count} 个错误、${data.warning_count} 个提示`
          : `校验完成：${data.warning_count} 个提示，无阻断错误`,
      );
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  const openConfirm = () => {
    setConfirmError(null);
    setNeedForce(Boolean(report?.force_required));
    setForce(false);
    setAnchorDocId((report?.generation as { anchor_doc_id?: string } | undefined)?.anchor_doc_id);
    setConfirmOpen(true);
  };

  const runConfirm = async () => {
    setBusy('generate');
    setConfirmError(null);
    try {
      const result = await confirmStructuredGroup(groupId, { anchorDocId, force });
      const stats = result.stats as { anchor_file?: string; anomaly_count?: number };
      message.success(
        `已生成 ${result.record_count} 条关联知识记录${
          stats.anchor_file ? `（锚点：${stats.anchor_file}）` : ''
        }${stats.anomaly_count ? `，${stats.anomaly_count} 条带提示` : ''}`,
      );
      setConfirmOpen(false);
      await loadDetail();
    } catch (err) {
      const text = describeApiError(err);
      setConfirmError(text);
      // 后端对「外键对不上」要求显式 force：把提示留在弹窗里，让用户知情后再选
      if (text.includes('force')) setNeedForce(true);
    } finally {
      setBusy(null);
    }
  };

  const runRevectorize = async () => {
    setBusy('revectorize');
    try {
      const updated = await revectorizeStructuredGroup(groupId);
      message.success(
        `已派发重新向量化（${vectorStatusMeta(updated.vector_status).label}），完成后自动刷新`,
      );
      await loadDetail();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  const submitAlias = async () => {
    if (!aliasTarget) return;
    setBusy('alias');
    try {
      await setStructuredGroupFileAlias(groupId, aliasTarget.doc_id, aliasValue.trim());
      message.success('角色名已更新');
      setAliasTarget(null);
      await loadDetail();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  const removeFile = async (file: StructuredGroupFile) => {
    setBusy(`remove-${file.doc_id}`);
    try {
      await removeStructuredGroupFile(groupId, file.doc_id);
      message.success(`「${fileLabel(file, file.doc_id)}」已移出文件组，引用它的关系已一并清理`);
      await loadDetail();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  const openAddFiles = async () => {
    setAddFilesSelected([]);
    setAddFilesOpen(true);
    try {
      const page = await fetchDocumentsPage({
        kbId: detail?.group.kb_id ?? undefined,
        space: 'structured',
        limit: 500,
      });
      const inGroup = new Set(files.map((f) => f.doc_id));
      setAddFilesOptions(page.items.filter((d) => !inGroup.has(d.id)));
    } catch (err) {
      message.error(describeApiError(err));
      setAddFilesOptions([]);
    }
  };

  const submitAddFiles = async () => {
    if (!addFilesSelected.length) {
      message.warning('请选择要补充进来的结构化文件');
      return;
    }
    setBusy('addFiles');
    try {
      await addStructuredGroupFiles(groupId, addFilesSelected);
      message.success('已补充文件，关系配置需要重新校验');
      setAddFilesOpen(false);
      await loadDetail();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setBusy(null);
    }
  };

  if (loading && !detail) {
    return (
      <div style={{ textAlign: 'center', padding: 48 }}>
        <Spin />
      </div>
    );
  }
  if (!detail) {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>
          返回文件组列表
        </Button>
        <Alert type="error" showIcon message={error ?? '文件组加载失败'} />
      </Space>
    );
  }

  const group = detail.group;
  const pendingParse = files.filter((f) => !f.removed && f.parse_status !== 'success');
  const removedFiles = files.filter((f) => f.removed);
  const usableFiles = files.filter((f) => !f.removed);

  const fileColumns: ColumnsType<StructuredGroupFile> = [
    {
      title: '角色名',
      dataIndex: 'alias',
      width: 150,
      render: (alias: string, row) => (
        <Space size={4}>
          <span>{alias || '-'}</span>
          <Button
            size="small"
            type="link"
            onClick={() => {
              setAliasTarget(row);
              setAliasValue(row.alias);
            }}
          >
            编辑
          </Button>
        </Space>
      ),
    },
    {
      title: '文件',
      key: 'file',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{row.current_file_name || row.file_name}</Typography.Text>
          {row.file_name !== row.current_file_name && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              建组时：{row.file_name}
            </Typography.Text>
          )}
          {row.removed && <Tag color="error">文件已删除/在回收站</Tag>}
        </Space>
      ),
    },
    {
      title: '格式',
      dataIndex: 'detected_format',
      width: 90,
      render: (f: string) => (f ? f.toUpperCase() : '-'),
    },
    {
      title: '解析状态',
      dataIndex: 'parse_status',
      width: 130,
      render: (s: string, row) => parseStatusTag(s, row.record_count, row.error_message),
    },
    { title: '记录数', dataIndex: 'record_count', width: 90 },
    {
      title: '异常',
      dataIndex: 'anomaly_count',
      width: 80,
      render: (n: number) => (n > 0 ? <Tag color="orange">{n}</Tag> : '0'),
    },
    {
      title: '字段数',
      key: 'fields',
      width: 80,
      render: (_, row) => row.fields.length,
    },
    {
      title: '主键候选（仍需确认）',
      key: 'keys',
      width: 230,
      render: (_, row) =>
        row.key_candidates.length ? (
          <Space size={4} wrap>
            {row.key_candidates.slice(0, 4).map((c) => (
              <Tooltip key={c.field} title={c.reason}>
                <Tag color={c.unique ? 'green' : 'default'}>
                  {c.field}
                  {c.unique ? ' · 唯一' : ''}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">-</Typography.Text>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 90,
      render: (_, row) => (
        <Popconfirm
          title="从文件组移出该文件？"
          description="不删除原始文件；引用它的关联关系会一并清理，避免留下无效关系。"
          okText="移出"
          onConfirm={() => void removeFile(row)}
        >
          <Button size="small" type="link" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  const relationColumns: ColumnsType<StructuredRelation> = [
    {
      title: '关联关系',
      key: 'rel',
      render: (_, row) => (
        <Typography.Text>
          {label(row.from_doc_id)}.<b>{row.from_field}</b> → {label(row.to_doc_id)}.
          <b>{row.to_field}</b>
        </Typography.Text>
      ),
    },
    {
      title: '关联类型',
      dataIndex: 'kind',
      width: 130,
      render: (kind: string, row) => (
        <Select
          size="small"
          style={{ width: 110 }}
          value={kind}
          options={KIND_OPTIONS}
          disabled={busy === `kind-${row.id}`}
          onChange={(v) => void updateRelationKind(row, v)}
        />
      ),
    },
    {
      title: '来源',
      dataIndex: 'origin',
      width: 130,
      render: (origin: string, row) => (
        <Tooltip title={row.reason || undefined}>
          <Tag color={origin === 'suggested' ? (row.confirmed ? 'blue' : 'gold') : 'purple'}>
            {origin === 'suggested'
              ? row.confirmed
                ? '系统推测'
                : '系统预填·待确认'
              : '用户配置'}
          </Tag>
        </Tooltip>
      ),
    },
    {
      title: '评分',
      key: 'score',
      width: 150,
      render: (_, row) => (
        <Tooltip title={(row.evidence ?? []).join('；') || row.reason || undefined}>
          <span>
            {relationScoreTag(row.score, row.tier)}
            {row.coverage != null && (
              <Typography.Text type="secondary" style={{ fontSize: 12, marginInlineStart: 6 }}>
                覆盖 {Math.round(row.coverage * 100)}%
              </Typography.Text>
            )}
          </span>
        </Tooltip>
      ),
    },
    {
      title: '确认',
      key: 'confirmed',
      width: 110,
      render: (_, row) => (
        <Checkbox
          checked={row.confirmed}
          disabled={busy === `confirm-${row.id}`}
          onChange={(e) => void toggleConfirm(row, e.target.checked)}
        >
          {row.confirmed ? '已确认' : '未确认'}
        </Checkbox>
      ),
    },
    {
      title: '操作',
      key: 'ops',
      width: 80,
      render: (_, row) => (
        <Button
          size="small"
          type="link"
          danger
          loading={busy === `drop-${row.id}`}
          onClick={() => void dropRelation(row)}
        >
          删除
        </Button>
      ),
    },
  ];

  const candidateColumns: ColumnsType<StructuredRelation> = [
    {
      title: '候选关系',
      key: 'rel',
      render: (_, row) => (
        <Typography.Text>
          {label(row.from_doc_id)}.<b>{row.from_field}</b> → {label(row.to_doc_id)}.
          <b>{row.to_field}</b>
        </Typography.Text>
      ),
    },
    {
      title: '推测类型',
      key: 'kind',
      width: 130,
      render: (_, row) => (
        <Select
          size="small"
          style={{ width: 110 }}
          value={candidateKinds[row.id] ?? String(row.kind)}
          options={KIND_OPTIONS}
          disabled={row.prefilled || relationExists(row.id)}
          onChange={(v) => setCandidateKinds((prev) => ({ ...prev, [row.id]: v }))}
        />
      ),
    },
    {
      title: '评分',
      key: 'score',
      width: 110,
      render: (_, row) => (
        <Tooltip title={(row.warnings ?? []).join('；') || undefined}>
          {relationScoreTag(row.score, row.tier)}
        </Tooltip>
      ),
    },
    {
      title: '依据',
      dataIndex: 'reason',
      render: (reason: string | undefined, row) => (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {(row.evidence?.length ? row.evidence.join('；') : reason) || '-'}
          {row.warnings?.length ? `（${row.warnings.join('；')}）` : ''}
        </Typography.Text>
      ),
    },
    {
      title: '采纳',
      key: 'adopt',
      width: 90,
      render: (_, row) =>
        row.prefilled || relationExists(row.id) ? (
          <Tag color="gold">已预填</Tag>
        ) : (
          <Checkbox
            checked={Boolean(adopted[row.id])}
            onChange={(e) => setAdopted((prev) => ({ ...prev, [row.id]: e.target.checked }))}
          />
        ),
    },
  ];

  const statColumns: ColumnsType<RelationStatRow> = [
    {
      title: '关系',
      key: 'rel',
      render: (_, row) => (
        <Typography.Text style={{ fontSize: 12 }}>
          {row.from_file}.<b>{row.from_field}</b> → {row.to_file}.<b>{row.to_field}</b>
        </Typography.Text>
      ),
    },
    {
      title: '类型',
      dataIndex: 'kind',
      width: 90,
      render: (kind: string) => KIND_LABEL[kind] ?? kind,
    },
    { title: '左行', dataIndex: 'left_rows', width: 66 },
    { title: '右行', dataIndex: 'right_rows', width: 66 },
    {
      title: '匹配',
      dataIndex: 'matched',
      width: 66,
      render: (n: number) => <Tag color="green">{n}</Tag>,
    },
    {
      title: '未匹配',
      dataIndex: 'unmatched',
      width: 80,
      render: (n: number) => (n ? <Tag color="red">{n}</Tag> : '0'),
    },
    {
      title: '空关联键',
      dataIndex: 'empty_key',
      width: 90,
      render: (n: number) => (n ? <Tag color="orange">{n}</Tag> : '0'),
    },
    {
      title: '主键侧重复',
      key: 'dup_left',
      width: 110,
      render: (_, row) =>
        row.left_duplicate_keys
          ? `${row.left_duplicate_keys} 键 / ${row.left_duplicate_rows} 行`
          : '0',
    },
    {
      title: '另一侧重复',
      key: 'dup_right',
      width: 110,
      render: (_, row) =>
        row.duplicate_keys ? `${row.duplicate_keys} 键 / ${row.duplicate_key_rows} 行` : '0',
    },
    {
      title: '孤立记录',
      dataIndex: 'orphan_right_rows',
      width: 90,
      render: (n: number) => (n ? <Tag color="orange">{n}</Tag> : '0'),
    },
  ];

  const stats = ((report?.relation_stats ?? []) as unknown) as RelationStatRow[];
  // 校验报告内部数组一律按数组消费：缺字段 / null 时退化为空数组
  const reportIssues = report?.issues ?? [];

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>
          返回列表
        </Button>
        <Typography.Title level={5} style={{ margin: 0 }}>
          {group.name}
        </Typography.Title>
        {groupStatusTag(group)}
        <Tag>{group.file_count} 个文件</Tag>
        <Tag>{group.relation_count} 条关系</Tag>
        {group.unconfirmed_count > 0 && (
          <Tag color="orange">{group.unconfirmed_count} 条待确认</Tag>
        )}
        <Tag color={group.record_count ? 'success' : 'default'}>
          {group.record_count} 条关联记录
        </Tag>
      </Space>

      {group.error_message && (
        <Alert type="warning" showIcon message={group.error_message} />
      )}
      {removedFiles.length > 0 && (
        <Alert
          type={usableFiles.length < 2 ? 'error' : 'warning'}
          showIcon
          message={
            usableFiles.length < 2
              ? `可用文件不足 2 个（${usableFiles.length}/${files.length}），关联关系无法成立`
              : `${removedFiles.length} 个文件已删除或在回收站，已有校验结果需重新确认`
          }
          description={
            <>
              缺失文件：{removedFiles.map((f) => fileLabel(f, f.doc_id)).join('、')}。
              {usableFiles.length < 2
                ? '请到文档回收站恢复文件，或通过「补充文件」加入其他结构化文件；恢复后点击「刷新结构」并重新校验。'
                : '恢复文件或调整关系后，请点击「刷新结构」并重新校验；校验通过前不能确认生成。'}
            </>
          }
        />
      )}
      {error && <Alert type="error" showIcon message={error} />}

      <ProcessingOverviewPanel
        view={processing.view}
        error={processing.error}
        title="处理概览（文件组）"
        extra={
          processing.view?.next_action === 'suggest' ? (
            <Space>
              <Button
                size="small"
                type="primary"
                icon={<ThunderboltOutlined />}
                loading={busy === 'suggest'}
                disabled={files.length < 2}
                onClick={() => void runSuggest()}
              >
                推测跨文件关系
              </Button>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                不会自动运行；结果为空时仍可在「关系配置」手动选择字段
              </Typography.Text>
            </Space>
          ) : null
        }
      />

      <Tabs
        defaultActiveKey="files"
        items={[
          {
            key: 'files',
            label: '文件与结构',
            children: (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Space wrap>
                  <Button icon={<PlusOutlined />} onClick={() => void openAddFiles()}>
                    补充文件
                  </Button>
                  <Button icon={<ReloadOutlined />} onClick={() => void loadDetail()}>
                    刷新结构
                  </Button>
                </Space>
                {pendingParse.length > 0 && (
                  <Alert
                    type="info"
                    showIcon
                    message="以下文件还没有解析结果，推测关系前需要全部解析成功"
                    description={pendingParse
                      .map(
                        (f) =>
                          `${fileLabel(f, f.doc_id)}（${structuredParseStatusMeta(f.parse_status).label}）`,
                      )
                      .join('、')}
                  />
                )}
                <Table
                  rowKey="doc_id"
                  size="small"
                  columns={fileColumns}
                  dataSource={files}
                  pagination={false}
                  expandable={{
                    expandedRowRender: (row) => (
                      <Table
                        rowKey={(f) => `${f.sheet ?? ''}::${f.name}`}
                        size="small"
                        pagination={false}
                        dataSource={row.field_specs}
                        columns={fieldSpecColumns}
                      />
                    ),
                  }}
                />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  字段类型与主键候选是系统按数据推断的结果，仅用于帮你定位关系，
                  不构成已确认的关联；原始数据不会被本功能修改。
                </Typography.Text>
              </Space>
            ),
          },
          {
            key: 'relations',
            label: `关系配置${group.unconfirmed_count ? `（${group.unconfirmed_count} 待确认）` : ''}`,
            children: (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Space wrap>
                  <Button
                    icon={<ThunderboltOutlined />}
                    loading={busy === 'suggest'}
                    disabled={files.length < 2}
                    onClick={() => void runSuggest()}
                  >
                    推测跨文件关系
                  </Button>
                  <Button
                    type="primary"
                    disabled={!candidates.some((c) => adopted[c.id])}
                    loading={busy === 'adopt'}
                    onClick={() => void adoptCandidates()}
                  >
                    保存勾选的关系（{candidates.filter((c) => adopted[c.id]).length}）
                  </Button>
                </Space>

                {candidates.length > 0 && (
                  <>
                    <Alert
                      type="info"
                      showIcon
                      message="评分 ≥90 自动预填，仍需逐条确认"
                      description="评分是「值匹配 + 字段名 + 类型 + 唯一性」的 0–100 规则打分，不是概率。≥90 会自动预填到下方关系表（未确认）；70–89 请自行勾选采纳；<70 仅供参考。仅字段名相似不代表真的有关联，未确认的关系不参与生成。"
                    />
                    <Table
                      rowKey="id"
                      size="small"
                      columns={candidateColumns}
                      dataSource={candidates}
                      pagination={false}
                    />
                  </>
                )}

                <Table
                  rowKey="id"
                  size="small"
                  columns={relationColumns}
                  dataSource={detail.relations}
                  pagination={false}
                  locale={{ emptyText: <Empty description="还没有关联关系" /> }}
                />

                <Typography.Text strong>手动配置一条关系（字段不确定时自己选）</Typography.Text>
                <Space wrap>
                  <Select
                    style={{ width: 180 }}
                    placeholder="起点文件（外键/「多」侧）"
                    value={manual.fromDoc || undefined}
                    options={files.map((f) => ({ value: f.doc_id, label: fileLabel(f, f.doc_id) }))}
                    onChange={(v) =>
                      setManual((m) => ({ ...m, fromDoc: v, fromField: '', toDoc: m.toDoc === v ? '' : m.toDoc }))
                    }
                  />
                  <Select
                    style={{ width: 160 }}
                    placeholder="关联字段"
                    showSearch
                    value={manual.fromField || undefined}
                    options={(fileById.get(manual.fromDoc)?.fields ?? []).map((f) => ({
                      value: f,
                      label: f,
                    }))}
                    onChange={(v) => setManual((m) => ({ ...m, fromField: v }))}
                  />
                  <Typography.Text type="secondary">→</Typography.Text>
                  <Select
                    style={{ width: 180 }}
                    placeholder="目标文件（主键/「一」侧）"
                    value={manual.toDoc || undefined}
                    options={files.map((f) => ({ value: f.doc_id, label: fileLabel(f, f.doc_id) }))}
                    onChange={(v) =>
                      setManual((m) => ({ ...m, toDoc: v, toField: '', fromDoc: m.fromDoc === v ? '' : m.fromDoc }))
                    }
                  />
                  <Select
                    style={{ width: 160 }}
                    placeholder="关联字段"
                    showSearch
                    value={manual.toField || undefined}
                    options={(fileById.get(manual.toDoc)?.fields ?? []).map((f) => ({
                      value: f,
                      label: f,
                    }))}
                    onChange={(v) => setManual((m) => ({ ...m, toField: v }))}
                  />
                  <Select
                    style={{ width: 110 }}
                    value={manual.kind}
                    options={KIND_OPTIONS}
                    onChange={(v) => setManual((m) => ({ ...m, kind: v }))}
                  />
                  <Button
                    icon={<PlusOutlined />}
                    loading={busy === 'manual'}
                    onClick={() => void addManualRelation()}
                  >
                    添加关系
                  </Button>
                </Space>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  起点 = 外键所在的「多」侧文件，目标 = 主键所在的「一」侧文件：用起点的字段值去
                  目标文件里查找对应记录（典型如选课表 → 学生表）。方向反了就交换两侧。
                  任何配置改动都会作废校验报告，需要重新校验并确认。
                </Typography.Text>
              </Space>
            ),
          },
          {
            key: 'validate',
            label: '校验报告',
            children: (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Space wrap>
                  <Button
                    icon={<SafetyCertificateOutlined />}
                    loading={busy === 'validate'}
                    disabled={!detail.relations.length}
                    onClick={() => void runValidate()}
                  >
                    运行校验
                  </Button>
                  {report?.checked_at && (
                    <Typography.Text type="secondary">
                      上次校验：{new Date(report.checked_at).toLocaleString()}
                    </Typography.Text>
                  )}
                </Space>
                {!detail.relations.length && (
                  <Alert type="warning" showIcon message="还没有关联关系，先去「关系配置」。" />
                )}
                {report && (
                  <>
                    <Space size={6} wrap>
                      <Tag color={report.error_count ? 'error' : 'success'}>
                        错误 {report.error_count}
                      </Tag>
                      <Tag color={report.warning_count ? 'warning' : 'default'}>
                        提示 {report.warning_count}
                      </Tag>
                      {report.structural_error_count > 0 && (
                        <Tag color="error">结构错误 {report.structural_error_count}</Tag>
                      )}
                      {report.unmatched_error_count > 0 && (
                        <Tag color="error">外键无法匹配 {report.unmatched_error_count}</Tag>
                      )}
                      {report.unconfirmed_count > 0 && (
                        <Tag color="orange">未确认关系 {report.unconfirmed_count}</Tag>
                      )}
                      <Tag color={report.can_confirm ? 'success' : 'default'}>
                        {report.can_confirm ? '可以确认生成' : '暂不可确认生成'}
                      </Tag>
                    </Space>
                    {reportIssues.map((issue, index) => (
                      <Alert
                        key={`${issue.relation_id}-${issue.type}-${index}`}
                        type={
                          issue.level === 'error'
                            ? 'error'
                            : issue.level === 'warning'
                              ? 'warning'
                              : 'info'
                        }
                        showIcon
                        message={`${issue.type}：${issue.message}`}
                      />
                    ))}
                    <Table
                      rowKey="relation_id"
                      size="small"
                      columns={statColumns}
                      dataSource={stats}
                      pagination={false}
                      scroll={{ x: 900 }}
                    />
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      校验只报告问题，不修改、不丢弃任何原始记录；未匹配的行会在关联记录里带提示保留。
                    </Typography.Text>
                  </>
                )}
              </Space>
            ),
          },
          {
            key: 'records',
            label: '关联记录',
            children: (
              <RecordsTab
                groupId={groupId}
                files={files}
                group={group}
                busy={busy === 'generate'}
                revectorizing={busy === 'revectorize'}
                onGenerate={openConfirm}
                onRevectorize={() => void runRevectorize()}
              />
            ),
          },
        ]}
      />

      <Modal
        title="确认关系并生成关联记录"
        open={confirmOpen}
        onCancel={() => setConfirmOpen(false)}
        onOk={() => void runConfirm()}
        okText="生成"
        okButtonProps={{ disabled: needForce && !force }}
        confirmLoading={busy === 'generate'}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            message="按已确认的关系整份生成"
            description="生成会替换该文件组此前的关联记录；过程中出错则保留原有结果，不产生半套数据。"
          />
          <div>
            <Typography.Text>锚点文件（以谁为主语组织知识文本）</Typography.Text>
            <Select
              style={{ width: '100%', marginTop: 6 }}
              placeholder="留空 = 自动选取向外关联最多的文件"
              allowClear
              value={anchorDocId}
              options={files.map((f) => ({ value: f.doc_id, label: fileLabel(f, f.doc_id) }))}
              onChange={(v) => setAnchorDocId(v)}
            />
          </div>
          {needForce && (
            <Checkbox checked={force} onChange={(e) => setForce(e.target.checked)}>
              已知情：存在外键无法匹配的原始记录，仍要生成（这些行会带提示保留，不会被丢弃或改写）
            </Checkbox>
          )}
          {confirmError && <Alert type="error" showIcon message={confirmError} />}
        </Space>
      </Modal>

      <Modal
        title="文件角色名"
        open={aliasTarget !== null}
        onCancel={() => setAliasTarget(null)}
        onOk={() => void submitAlias()}
        okButtonProps={{ loading: busy === 'alias' }}
        okText="保存"
      >
        <Input
          value={aliasValue}
          maxLength={40}
          placeholder="如「学生表」「选课表」「药性属性表」"
          onChange={(e) => setAliasValue(e.target.value)}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          只影响关联知识文本的可读性与来源标注，不改文件名。
        </Typography.Text>
      </Modal>

      <Modal
        title="补充文件到该组"
        open={addFilesOpen}
        onCancel={() => setAddFilesOpen(false)}
        onOk={() => void submitAddFiles()}
        okButtonProps={{ loading: busy === 'addFiles' }}
        okText="添加"
        width={640}
      >
        <Select
          mode="multiple"
          style={{ width: '100%' }}
          placeholder="选择本知识库结构化数据空间里的文件"
          showSearch
          optionFilterProp="label"
          value={addFilesSelected}
          options={addFilesOptions.map((d) => ({
            value: d.id,
            label: `${d.file_name}（${(d.structured_status ?? '-') + ' / ' + d.file_type}）`,
          }))}
          onChange={(v) => setAddFilesSelected(v)}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          组构成变化后原有校验立即作废，需要重新校验并确认。
        </Typography.Text>
      </Modal>
    </Space>
  );
}

const fieldSpecColumns: ColumnsType<StructuredField> = [
  {
    title: '字段',
    dataIndex: 'name',
    width: 180,
    render: (name: string) => <span style={{ fontWeight: 500 }}>{name}</span>,
  },
  {
    title: '推断类型',
    dataIndex: 'type',
    width: 100,
    render: (type: string) => <Tag>{fieldTypeLabel(type)}</Tag>,
  },
  { title: '出现次数', dataIndex: 'count', width: 90 },
  {
    title: '空值数',
    dataIndex: 'null_count',
    width: 80,
    render: (n: number) => (n > 0 ? <Tag color="orange">{n}</Tag> : '0'),
  },
  {
    title: '工作表',
    dataIndex: 'sheet',
    width: 110,
    render: (sheet?: string | null) => sheet || '-',
  },
  { title: '说明', dataIndex: 'note', render: (note?: string | null) => note || '-' },
];

/** 去掉 id 后才能进入 PUT /relations（后端按字段重新编号），评分/依据随关系保留。 */
function toRelationPayload(rel: StructuredRelation) {
  return {
    from_doc_id: rel.from_doc_id,
    from_field: rel.from_field,
    to_doc_id: rel.to_doc_id,
    to_field: rel.to_field,
    kind: rel.kind,
    confirmed: rel.confirmed,
    origin: rel.origin,
    confidence: rel.confidence ?? null,
    score: rel.score ?? null,
    tier: rel.tier ?? null,
    coverage: rel.coverage ?? undefined,
    row_coverage: rel.row_coverage ?? undefined,
    evidence: rel.evidence ?? undefined,
    reason: rel.reason ?? '',
  };
}

/** 关联记录页：确认后才有内容；分页浏览 + 单条追溯详情。 */
function RecordsTab({
  groupId,
  files,
  group,
  busy,
  revectorizing,
  onGenerate,
  onRevectorize,
}: {
  groupId: string;
  files: StructuredGroupFile[];
  group: StructuredGroup;
  busy: boolean;
  revectorizing: boolean;
  onGenerate: () => void;
  onRevectorize: () => void;
}) {
  const [items, setItems] = useState<StructuredGroupRecordItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [keyword, setKeyword] = useState('');
  const [keywordDraft, setKeywordDraft] = useState('');
  const [onlyAnomaly, setOnlyAnomaly] = useState(false);
  const [refreshSeq, setRefreshSeq] = useState(0);
  const [detail, setDetail] = useState<StructuredGroupRecordDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const idRef = useRef<string | null>(null);

  useEffect(() => {
    idRef.current = groupId;
    setPage(1);
    setKeyword('');
    setKeywordDraft('');
    setOnlyAnomaly(false);
    setDetail(null);
    setRefreshSeq((v) => v + 1);
  }, [groupId]);

  useEffect(() => {
    if (!group.record_count) {
      setItems([]);
      setTotal(0);
    }
  }, [group.record_count]);

  useEffect(() => {
    const offset = (page - 1) * pageSize;
    setLoading(true);
    void fetchStructuredGroupRecords(groupId, {
      limit: pageSize,
      offset,
      keyword: keyword.trim() || undefined,
      onlyAnomaly,
    })
      .then((data) => {
        if (idRef.current !== groupId) return;
        setItems(data.items);
        setTotal(data.total);
      })
      .catch((err) => {
        if (idRef.current === groupId) message.error(describeApiError(err));
      })
      .finally(() => {
        if (idRef.current === groupId) setLoading(false);
      });
  }, [groupId, page, pageSize, keyword, onlyAnomaly, refreshSeq, group.record_count, group.status]);

  const openDetail = async (row: StructuredGroupRecordItem) => {
    setDetailLoading(true);
    try {
      const data = await fetchStructuredGroupRecord(groupId, row.id);
      if (idRef.current !== groupId) return;
      setDetail(data);
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setDetailLoading(false);
    }
  };

  const columns: ColumnsType<StructuredGroupRecordItem> = [
    { title: '#', dataIndex: 'seq_no', width: 60 },
    {
      title: '关联知识记录',
      dataIndex: 'knowledge_text',
      render: (text: string) => (
        <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
          {text}
        </Typography.Paragraph>
      ),
    },
    {
      title: '锚点',
      dataIndex: 'anchor_doc_id',
      width: 130,
      render: (docId?: string | null) => {
        const file = files.find((f) => f.doc_id === docId);
        return file ? fileLabel(file, file.doc_id) : '-';
      },
    },
    { title: '来源', dataIndex: 'source_count', width: 70 },
    { title: '关系', dataIndex: 'relation_count', width: 70 },
    {
      title: '提示',
      dataIndex: 'anomaly_count',
      width: 70,
      render: (n: number) => (n ? <Tag color="orange">{n}</Tag> : '0'),
    },
    {
      title: '操作',
      key: 'ops',
      width: 100,
      render: (_, row) => (
        <Button size="small" type="link" loading={detailLoading} onClick={() => void openDetail(row)}>
          来源追溯
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Button
          type="primary"
          icon={<SafetyCertificateOutlined />}
          loading={busy}
          onClick={onGenerate}
        >
          {group.record_count ? '按已确认关系重新生成' : '确认关系并生成关联记录'}
        </Button>
        {group.record_count > 0 && group.status === 'confirmed' && (
          <>
            <Button
              icon={<CloudSyncOutlined />}
              loading={revectorizing}
              disabled={group.vector_status === 'indexing'}
              onClick={onRevectorize}
            >
              重新向量化
            </Button>
            <Tooltip
              title={
                group.vector_error ||
                '一条关联记录一个向量；重建不会产生重复向量，失败时旧向量保留'
              }
            >
              <Tag color={vectorStatusMeta(group.vector_status).color} style={{ margin: 0 }}>
                {vectorStatusMeta(group.vector_status).label}
                {group.vector_count > 0 ? ` · ${group.vector_count} 条` : ''}
              </Tag>
            </Tooltip>
          </>
        )}
        <Input.Search
          style={{ width: 220 }}
          placeholder="按知识文本搜索"
          allowClear
          value={keywordDraft}
          onChange={(e) => setKeywordDraft(e.target.value)}
          onSearch={(v) => {
            setPage(1);
            setKeyword(v);
          }}
        />
        <Checkbox
          checked={onlyAnomaly}
          onChange={(e) => {
            setPage(1);
            setOnlyAnomaly(e.target.checked);
          }}
        >
          只看带提示的记录
        </Checkbox>
        <Button icon={<ReloadOutlined />} onClick={() => setRefreshSeq((v) => v + 1)}>
          刷新
        </Button>
      </Space>

      {!group.record_count && (
        <Alert
          type="info"
          showIcon
          message="还没有正式关联记录"
          description="关系需逐条确认、校验通过后才能生成；未确认的关系与结构错误都会阻断生成。"
        />
      )}
      {group.status === 'stale' && (
        <Alert
          type="warning"
          showIcon
          message="组内文件已被替换或删除，以下是作废前的记录"
          description={group.error_message || '请重新校验关系并再次确认后生成。'}
        />
      )}
      {group.status === 'confirmed' && group.record_count > 0 && group.vector_status === 'failed' && (
        <Alert
          type="error"
          showIcon
          message="向量化失败，此前已生效的向量保持不变"
          description={group.vector_error || '点击「重新向量化」重试；失败不会清空旧索引。'}
        />
      )}

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns}
        dataSource={items}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (n) => `共 ${n} 条`,
          onChange: (p, ps) => {
            setPage(p);
            setPageSize(ps);
          },
        }}
        locale={{ emptyText: <Empty description="无关联记录" /> }}
      />

      <Modal
        title="来源追溯"
        open={detail !== null}
        width={920}
        onCancel={() => setDetail(null)}
        footer={<Button onClick={() => setDetail(null)}>关闭</Button>}
      >
        {detail && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="文件组">{detail.group_name}</Descriptions.Item>
              <Descriptions.Item label="锚点文件">{detail.anchor_file || '-'}</Descriptions.Item>
              <Descriptions.Item label="序号">{detail.seq_no}</Descriptions.Item>
              <Descriptions.Item label="生成时间">
                {detail.created_at ? new Date(detail.created_at).toLocaleString() : '-'}
              </Descriptions.Item>
              <Descriptions.Item label="关联知识记录" span={2}>
                <Typography.Paragraph style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>
                  {detail.knowledge_text}
                </Typography.Paragraph>
              </Descriptions.Item>
            </Descriptions>

            {detail.anomalies.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message="本条记录的提示"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {detail.anomalies.map((a, i) => (
                      <li key={`${i}-${a.slice(0, 20)}`}>{a}</li>
                    ))}
                  </ul>
                }
              />
            )}

            <Typography.Text strong>参与关联的原始记录</Typography.Text>
            <Table
              rowKey={(row) => `${row.doc_id}-${row.locate}`}
              size="small"
              pagination={false}
              dataSource={detail.parts}
              columns={[
                {
                  title: '文件',
                  key: 'file',
                  width: 170,
                  render: (_, row) => row.alias || row.file_name,
                },
                { title: '定位', dataIndex: 'locate', width: 140 },
                {
                  title: '原始字段与值',
                  dataIndex: 'values',
                  render: (values: Record<string, unknown>) => (
                    <Typography.Text style={{ fontSize: 12 }}>
                      {Object.entries(values)
                        .map(([k, v]) => `${k}=${cellValueText(v)}`)
                        .join('；')}
                    </Typography.Text>
                  ),
                },
              ]}
            />

            <Typography.Text strong>关联字段与角色</Typography.Text>
            <Table
              rowKey={(row) => `${row.doc_id}-${row.record_id}-${row.role}`}
              size="small"
              pagination={false}
              dataSource={detail.sources}
              columns={[
                {
                  title: '原始文件',
                  key: 'file',
                  width: 200,
                  render: (_, row) => `${row.alias || row.file_name}`,
                },
                {
                  title: '定位',
                  key: 'locate',
                  width: 160,
                  render: (_, row) =>
                    row.json_path ||
                    `${row.sheet_name ? `${row.sheet_name} · ` : ''}第 ${row.row_no ?? 0} 行`,
                },
                { title: '角色', dataIndex: 'role', width: 110 },
                {
                  title: '参与关联的字段',
                  key: 'join',
                  render: (_, row) =>
                    row.join_field
                      ? `${row.join_from_field ? `${row.join_from_field} → ` : ''}${row.join_field}`
                      : '-',
                },
                {
                  title: '记录 ID',
                  dataIndex: 'record_id',
                  width: 120,
                  render: (id: string) => (
                    <Tooltip title={id}>
                      <Typography.Text style={{ fontSize: 12 }}>{id.slice(0, 8)}</Typography.Text>
                    </Tooltip>
                  ),
                },
              ]}
            />

            <Space size={4} wrap>
              <Typography.Text type="secondary">参与的关系 ID：</Typography.Text>
              {detail.relation_ids.map((id) => (
                <Tag key={id}>{id}</Tag>
              ))}
            </Space>
          </Space>
        )}
      </Modal>
    </Space>
  );
}
