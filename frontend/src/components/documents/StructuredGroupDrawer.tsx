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
  Progress,
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
  SaveOutlined,
  StopOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { RcFile, UploadFile } from 'antd/es/upload';
import type {
  GroupProcessingInfo,
  StructuredGroup,
  StructuredGroupDetail,
  StructuredGroupFile,
  StructuredGroupRecordDetail,
  StructuredGroupRecordItem,
  StructuredGroupValidation,
  StructuredRelation,
} from '@/types';
import {
  cancelStructuredGroupVectorize,
  confirmStructuredGroup,
  createStructuredGroup,
  deleteStructuredGroup,
  fetchGroupProcessing,
  fetchStructuredGroup,
  fetchStructuredGroupRecord,
  fetchStructuredGroupRecords,
  fetchStructuredGroups,
  renameStructuredGroup,
  revectorizeStructuredGroup,
  saveStructuredRelations,
  uploadDocument,
  validateStructuredGroup,
} from '@/services/api';
import { describeApiError } from '@/utils/apiError';
import { STRUCTURED_FORMATS } from '@/utils/documentFormats';
import {
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  cellValueText,
  structuredParseStatusMeta,
} from '@/utils/structuredDisplay';
import {
  groupRowStatus,
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

/** 文件在组里的展示名：角色名优先，其次当前文件名，最后建组时快照。 */
function fileLabel(file: StructuredGroupFile | undefined, docId?: string): string {
  if (!file || file.doc_id !== docId) return docId ?? '';
  return file.alias || file.current_file_name || file.file_name;
}

/** 关系配置里的表名：只保留纯表名，去掉文件夹路径与文件后缀（D1_TCM_terminology.csv → D1_TCM_terminology）。 */
function tableName(file: StructuredGroupFile | undefined, docId?: string): string {
  const base = fileLabel(file, docId).split(/[\\/]/).pop() ?? '';
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(0, dot) : base;
}

interface Props {
  kbId: string | null;
  /** 新建文件组时上传文件落在哪个文件夹（与结构化列表当前目录一致）。 */
  folderId?: string | null;
  open: boolean;
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
  initialGroupId,
  onClose,
  onChanged,
}: Props) {
  const [groups, setGroups] = useState<StructuredGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  /** 手动触发向量化后 10 分钟内持续轮询列表，让进度条从 0% 走到 100%。 */
  const [vectorPollUntil, setVectorPollUntil] = useState(0);
  /** 正在派发「停止向量化」的文件组 id（按钮 loading 用）。 */
  const [stoppingGroupId, setStoppingGroupId] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<StructuredGroup | null>(null);
  const [renameValue, setRenameValue] = useState('');

  const loadGroups = useCallback(async (silent = false) => {
    if (!kbId) {
      setGroups([]);
      return;
    }
    // 轮询刷新用 silent，避免每 4 秒给表格闪一次 loading
    if (!silent) setLoading(true);
    try {
      setGroups(await fetchStructuredGroups(kbId));
      setError(null);
    } catch (err) {
      setError(describeApiError(err));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [kbId]);

  // 有文件组正在向量化时轮询列表：进度条要能看到百分比推进
  useEffect(() => {
    if (!open || detailId) return;
    if (!groups.some((g) => g.vector_status === 'indexing') && Date.now() >= vectorPollUntil)
      return;
    const t = setInterval(() => void loadGroups(true), 4_000);
    return () => clearInterval(t);
  }, [open, detailId, groups, loadGroups, vectorPollUntil]);

  useEffect(() => {
    if (!open) {
      setDetailId(null);
      setGroups([]);
      setError(null);
      setUploadOpen(false);
      return;
    }
    void loadGroups();
  }, [open, loadGroups]);

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

  /** 列表内直接开始向量化（不用进详情）：只写向量，不重新生成关联记录。 */
  const handleGroupVectorize = async (group: StructuredGroup) => {
    try {
      const updated = await revectorizeStructuredGroup(group.id);
      setVectorPollUntil(Date.now() + 10 * 60_000);
      message.success(
        `已开始向量化：${group.name}（${vectorStatusMeta(updated.vector_status).label}）`,
      );
      void loadGroups();
    } catch (err) {
      message.error(describeApiError(err));
    }
  };

  /** 停止向量化：后端在下一批记录之间退出，清理本次部分向量后回到待向量化。 */
  const handleGroupStop = async (group: StructuredGroup) => {
    setStoppingGroupId(group.id);
    try {
      const updated = await cancelStructuredGroupVectorize(group.id);
      setVectorPollUntil(Date.now() + 2 * 60_000);
      message.success(
        `已请求停止：${group.name}（${vectorStatusMeta(updated.vector_status).label}）`,
      );
      void loadGroups(true);
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setStoppingGroupId(null);
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
            <Tooltip title="历史遗留的未确认关系需要你逐条确认，未确认的不参与生成">
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
        const percent =
          row.vector_status === 'completed'
            ? 100
            : Math.min(
                100,
                Math.round((row.vector_count / Math.max(1, row.record_count)) * 100),
              );
        return (
          <Tooltip
            title={`${row.vector_error || meta.label}（${row.vector_count} / ${row.record_count} 条）`}
          >
            <div style={{ paddingTop: 4 }}>
              <Tag color={meta.color} style={{ margin: 0 }}>
                {meta.label}
                {row.vector_count > 0 ? ` · ${row.vector_count}` : ''}
              </Tag>
              {row.vector_status !== 'completed' && (
                <Progress percent={percent} size="small" style={{ margin: '4px 0 0' }} />
              )}
            </div>
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
          {row.status === 'confirmed' && row.record_count > 0 && (
            <Tooltip
              title={
                row.vector_status === 'completed'
                  ? '按现有关联记录重建向量'
                  : '为该文件组的每条关联记录写入向量'
              }
            >
              <Button
                size="small"
                type="link"
                disabled={row.vector_status === 'indexing'}
                onClick={() => void handleGroupVectorize(row)}
              >
                {row.vector_status === 'completed' ? '重新向量化' : '开始向量化'}
              </Button>
            </Tooltip>
          )}
          {row.status === 'confirmed' && row.vector_status === 'indexing' && (
            <Tooltip title="停止本次向量化（下一批记录之间退出，已写入的部分向量会被清理）">
              <Button
                size="small"
                type="link"
                danger
                icon={<StopOutlined />}
                loading={stoppingGroupId === row.id}
                onClick={() => void handleGroupStop(row)}
              >
                停止
              </Button>
            </Tooltip>
          )}
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
          {[
            { key: 'files', label: '选择文件（可多选）', extraProps: { multiple: true } },
            { key: 'folder', label: '选择文件夹', extraProps: { directory: true } },
          ].map((item) => (
            <Upload
              key={item.key}
              accept={GROUP_ACCEPT}
              fileList={fileList}
              beforeUpload={() => false}
              onChange={({ fileList: fl }) => setFileList(fl)}
              {...item.extraProps}
            >
              <Button icon={<PlusOutlined />}>{item.label}</Button>
            </Upload>
          ))}
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
  const [relRows, setRelRows] = useState<RelRow[]>([]);
  const [report, setReport] = useState<StructuredGroupValidation | null>(null);
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
    setReport(null);
    void loadDetail();
  }, [groupId, loadDetail]);

  /**
   * 处理概览（第五阶段）：文件组「上传 → 字段识别 → 配置关联字段 → 用户确认 → 校验 → 生成 → 完成」
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

  // detail 是关系配置的唯一回执：每次重读后按「已保存的关系 + 主键配置」重建编辑行
  useEffect(() => {
    setRelRows(buildRelRows(files, detail?.relations ?? []));
  }, [detail, files]);

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
  const patchRelRow = (key: string, patch: Partial<RelRow>) =>
    setRelRows((rows) => rows.map((r) => (r.key === key ? { ...r, ...patch } : r)));

  /** 同一表再添加一条外键行（复合主键表典型场景：多个字段分别指向多张维表）。 */
  const addFkRow = (row: RelRow) => {
    setRelRows((rows) => {
      const idx = rows.findIndex((r) => r.key === row.key);
      const next = [...rows];
      next.splice(idx + 1, 0, {
        key: nextRelRowKey(),
        docId: row.docId,
        pk: row.pk,
        pkType: row.pkType,
        hasFk: true,
        fkDocId: row.docId,
      });
      return next;
    });
  };

  const dropRelRow = (row: RelRow) =>
    setRelRows((rows) => rows.filter((r) => r.key !== row.key));

  /**
   * 整表校验后一次性保存：有外键的行 → 一条 manual 关系（from=外键字段所在表，to=引用表）。
   * 主键配置没有独立的后端字段，随关系 reason 的 JSON 一起保存以便回读；
   * 无外键的行不产生关系（其主键配置不落库）。
   */
  const saveRelRows = async () => {
    for (const row of relRows) {
      const name = label(row.docId);
      if (!row.pk.length) {
        message.warning(`请为「${name}」选择主键字段`);
        return;
      }
      if (row.pkType === 'composite' && row.pk.length < 2) {
        message.warning(`「${name}」是复合主键，请至少选择 2 个字段`);
        return;
      }
      if (row.hasFk) {
        if (!row.fkField || !row.fkDocId || !row.refField || !row.refDocId) {
          message.warning(
            `「${name}」声明了有外键，请补全外键字段、外键字段所在表、引用字段、引用表`,
          );
          return;
        }
        if (row.fkDocId === row.refDocId) {
          message.warning(`「${name}」的外键字段所在表与引用表不能是同一个表`);
          return;
        }
      }
    }
    setBusy('saveRelations');
    try {
      await saveStructuredRelations(groupId, [
        ...relRows.filter((r) => r.hasFk).map((r) => ({
          from_doc_id: r.fkDocId!,
          from_field: r.fkField!,
          to_doc_id: r.refDocId!,
          to_field: r.refField!,
          kind: 'many_to_one',
          confirmed: true,
          origin: 'manual',
          confidence: null,
          reason: JSON.stringify({ pkcfg: true, pk: r.pk, pkType: r.pkType }),
        })),
      ]);
      message.success('关系配置已保存，请重新校验');
      await loadDetail();
    } catch (err) {
      message.error(describeApiError(err));
      // 后端可能已经提交但响应失败：重读详情，避免继续用旧关系状态操作
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

  /** 某行的字段下拉选项（主键/外键字段/引用字段都从这里取）。 */
  const fieldOptions = (docId?: string) =>
    (docId ? fileById.get(docId)?.fields ?? [] : []).map((f) => ({ value: f, label: f }));
  const tableOptions = usableFiles.map((f) => ({
    value: f.doc_id,
    label: tableName(f, f.doc_id),
  }));
  const NO_FK_CELL = <Typography.Text type="secondary">-</Typography.Text>;

  const relConfigColumns: ColumnsType<RelRow> = [
    {
      title: '表名',
      key: 'doc',
      width: 180,
      render: (_, row) => (
        <Typography.Text>{tableName(fileById.get(row.docId), row.docId)}</Typography.Text>
      ),
      onCell: (row) => {
        // 同一表的多条外键共用一个表名单元格：首行纵向合并展开，其余行隐藏，名称居中
        const sameTable = relRows.filter((r) => r.docId === row.docId);
        return {
          rowSpan: sameTable[0]?.key === row.key ? sameTable.length : 0,
          style: { textAlign: 'center', verticalAlign: 'middle' },
        };
      },
    },
    {
      title: '主键',
      key: 'pk',
      width: 230,
      render: (_, row) => (
        <Select
          size="small"
          showSearch
          optionFilterProp="label"
          style={{ width: 210 }}
          mode={row.pkType === 'composite' ? 'multiple' : undefined}
          placeholder={row.pkType === 'composite' ? '选择 2 个及以上字段' : '选择主键字段'}
          value={row.pkType === 'composite' ? row.pk : row.pk[0]}
          options={fieldOptions(row.docId)}
          onChange={(v: string | string[]) =>
            patchRelRow(row.key, { pk: Array.isArray(v) ? v : v ? [v] : [] })
          }
        />
      ),
    },
    {
      title: '主键类型',
      key: 'pkType',
      width: 120,
      render: (_, row) => (
        <Select
          size="small"
          style={{ width: 110 }}
          value={row.pkType}
          options={[
            { value: 'single', label: '单字段主键' },
            { value: 'composite', label: '复合主键' },
          ]}
          onChange={(v: RelRow['pkType']) =>
            // 切回单字段时只保留第一个字段，避免残留多选值
            patchRelRow(row.key, { pkType: v, pk: v === 'single' ? row.pk.slice(0, 1) : row.pk })
          }
        />
      ),
    },
    {
      title: '有无外键',
      key: 'hasFk',
      width: 100,
      render: (_, row) => (
        <Select
          size="small"
          style={{ width: 80 }}
          value={row.hasFk ? 'yes' : 'no'}
          options={[
            { value: 'yes', label: '有' },
            { value: 'no', label: '无' },
          ]}
          onChange={(v) =>
            patchRelRow(
              row.key,
              v === 'yes'
                ? { hasFk: true, fkDocId: row.docId }
                : // 无外键 → 后四列不允许填写，直接清空
                  { hasFk: false, fkField: undefined, fkDocId: undefined, refField: undefined, refDocId: undefined },
            )
          }
        />
      ),
    },
    {
      title: '外键字段',
      key: 'fkField',
      width: 180,
      render: (_, row) =>
        !row.hasFk ? NO_FK_CELL : (
          <Select
            size="small"
            showSearch
            optionFilterProp="label"
            style={{ width: 160 }}
            placeholder="选择外键字段"
            value={row.fkField}
            options={fieldOptions(row.fkDocId ?? row.docId)}
            onChange={(v) => patchRelRow(row.key, { fkField: v })}
          />
        ),
    },
    {
      title: '外键字段所在表',
      key: 'fkDoc',
      width: 180,
      render: (_, row) =>
        !row.hasFk ? NO_FK_CELL : (
          <Select
            size="small"
            style={{ width: 160 }}
            value={row.fkDocId ?? row.docId}
            options={tableOptions}
            onChange={(v) =>
              // 换表后原字段未必存在，清空外键字段让用户重选
              patchRelRow(row.key, { fkDocId: v, fkField: undefined })
            }
          />
        ),
    },
    {
      title: '引用字段',
      key: 'refField',
      width: 180,
      render: (_, row) =>
        !row.hasFk ? NO_FK_CELL : (
          <Select
            size="small"
            showSearch
            optionFilterProp="label"
            style={{ width: 160 }}
            placeholder="选择引用字段"
            value={row.refField}
            options={fieldOptions(row.refDocId)}
            onChange={(v) => patchRelRow(row.key, { refField: v })}
          />
        ),
    },
    {
      title: '引用表',
      key: 'refDoc',
      width: 180,
      render: (_, row) =>
        !row.hasFk ? NO_FK_CELL : (
          <Select
            size="small"
            showSearch
            optionFilterProp="label"
            style={{ width: 160 }}
            placeholder="选择引用表"
            value={row.refDocId}
            options={tableOptions}
            onChange={(v) =>
              // 换引用表后原引用字段未必存在，清空让用户重选
              patchRelRow(row.key, { refDocId: v, refField: undefined })
            }
          />
        ),
    },
    {
      title: '操作',
      key: 'ops',
      width: 140,
      fixed: 'right',
      render: (_, row) => (
        <Space size={0}>
          <Button size="small" type="link" onClick={() => addFkRow(row)}>
            添加外键
          </Button>
          <Button
            size="small"
            type="link"
            danger
            disabled={relRows.filter((r) => r.docId === row.docId).length <= 1}
            onClick={() => dropRelRow(row)}
          >
            删除
          </Button>
        </Space>
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
                ? '请到文档回收站恢复文件；恢复后点击「刷新结构」并重新校验。'
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
          processing.view?.next_action === 'manual_relations' ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              请在「关系配置」中手动选择两侧文件和关联字段
            </Typography.Text>
          ) : null
        }
      />

      <Tabs
        defaultActiveKey="relations"
        items={[
          {
            key: 'relations',
            label: '关系配置',
            children: (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Alert
                  type="info"
                  showIcon
                  message="按表配置主键与外键关系"
                  description="每个表默认一行；一个表有多个外键时，点该行「添加外键」再配置一条。有无外键 = 有 时必须补全后面的外键字段、外键字段所在表、引用字段、引用表；= 无 时这四列不需要填。保存后请重新校验，至少 1 条外键关系才能生成关联记录。"
                />
                {pendingParse.length > 0 && (
                  <Alert
                    type="warning"
                    showIcon
                    message="以下文件还没有解析结果，字段列表为空，需全部解析成功后再配置"
                    description={pendingParse
                      .map(
                        (f) =>
                          `${fileLabel(f, f.doc_id)}（${structuredParseStatusMeta(f.parse_status).label}）`,
                      )
                      .join('、')}
                  />
                )}
                <Space wrap>
                  <Button
                    type="primary"
                    icon={<SaveOutlined />}
                    loading={busy === 'saveRelations'}
                    onClick={() => void saveRelRows()}
                  >
                    保存关系配置
                  </Button>
                  <Button icon={<ReloadOutlined />} onClick={() => void loadDetail()}>
                    刷新结构
                  </Button>
                </Space>
                <Table
                  rowKey="key"
                  size="small"
                  columns={relConfigColumns}
                  dataSource={relRows}
                  pagination={false}
                  scroll={{ x: 1350 }}
                />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  外键关系方向：外键字段（所在表）→ 引用字段（引用表），即用外键字段的值去引用表里查主键记录。
                  任何配置改动都会作废校验报告，保存后需要重新校验并确认。
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
    </Space>
  );
}

// ─────────────────────────── 关系配置表（文件组详情「关系配置」页签） ──────────

/**
 * 关系配置行：一个表（文件）至少一行；某表有多个外键时多行（每行配一条外键）。
 * 有外键的行保存为一条 manual 关系（from=外键字段所在表，to=引用表）。
 */
interface RelRow {
  key: string;
  /** 表名（组内文件 doc_id） */
  docId: string;
  /** 主键字段；单字段主键 1 个，复合主键 ≥2 个 */
  pk: string[];
  pkType: 'single' | 'composite';
  hasFk: boolean;
  fkField?: string;
  fkDocId?: string;
  refField?: string;
  refDocId?: string;
}

let relRowSeq = 0;
const nextRelRowKey = () => `rel-row-${++relRowSeq}`;

/** 主键配置没有独立的后端字段：随关系保存在 reason 的 JSON 里（pkcfg 标记），回读时还原。 */
function parsePkCfg(reason?: string): { pk: string[]; pkType: RelRow['pkType'] } | null {
  if (!reason) return null;
  try {
    const obj = JSON.parse(reason);
    if (obj && obj.pkcfg === true && Array.isArray(obj.pk)) {
      return { pk: obj.pk as string[], pkType: obj.pkType === 'composite' ? 'composite' : 'single' };
    }
  } catch {
    // 旧数据 reason 是纯文本，忽略
  }
  return null;
}

/**
 * 由文件组详情重建编辑行：每个未移出文件一行；
 * 以该文件为外键侧的关系逐条展开为独立行（多外键 = 多行），并回读主键配置。
 */
function buildRelRows(
  files: StructuredGroupFile[],
  relations: StructuredRelation[],
): RelRow[] {
  const rows: RelRow[] = [];
  for (const f of files.filter((item) => !item.removed)) {
    // 预填主键：优先系统识别出的唯一主键候选，用户仍可修改
    const pkGuess = f.key_candidates.find((c) => c.unique)?.field ?? f.key_candidates[0]?.field ?? '';
    const fkRels = relations.filter((r) => r.from_doc_id === f.doc_id);
    if (!fkRels.length) {
      rows.push({
        key: nextRelRowKey(),
        docId: f.doc_id,
        pk: pkGuess ? [pkGuess] : [],
        pkType: 'single',
        hasFk: false,
      });
      continue;
    }
    fkRels.forEach((r, i) => {
      const cfg = parsePkCfg(r.reason);
      rows.push({
        key: nextRelRowKey(),
        docId: f.doc_id,
        pk: cfg?.pk ?? (i === 0 && pkGuess ? [pkGuess] : []),
        pkType: cfg?.pkType ?? 'single',
        hasFk: true,
        fkField: r.from_field,
        fkDocId: r.from_doc_id,
        refField: r.to_field,
        refDocId: r.to_doc_id,
      });
    });
  }
  return rows;
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
