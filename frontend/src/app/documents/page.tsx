'use client';

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import {
  Alert,
  Breadcrumb,
  Button,
  Dropdown,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
  type UploadProps,
} from 'antd';
import {
  ArrowLeftOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  EllipsisOutlined,
  EyeOutlined,
  FileTextOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  FolderOutlined,
  ExportOutlined,
  ReloadOutlined,
  StopOutlined,
  SwapOutlined,
  TableOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { RcFile, UploadFile } from 'antd/es/upload';
import type { DocumentItem, FileFolderItem, FileSpace, ParseStatus } from '@/types';
import {
  cancelStructuredVectorize,
  createFolder,
  deleteDocument,
  deleteFolder,
  fetchDocument,
  fetchDocumentContent,
  moveDocument,
  renameDocument,
  reparseStructured,
  replaceDocument,
  revectorizeStructured,
  updateFolder,
  uploadDocument,
} from '@/services/api';
import { isParsing, useDocumentStore } from '@/stores/documentStore';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import { DocumentPreviewDrawer } from '@/components/documents/DocumentPreviewDrawer';
import { StructuredPreviewDrawer } from '@/components/documents/StructuredPreviewDrawer';
import { StructuredGroupsDrawer } from '@/components/documents/StructuredGroupDrawer';
import { downloadBlob } from '@/utils/download';
import { describeApiError } from '@/utils/apiError';
import { ProcessingStatusTag } from '@/components/common/ProcessingStatusTag';
import { documentRowStatus, structuredRowStatus } from '@/utils/processingStatus';
import { structuredParseStatusMeta } from '@/utils/structuredDisplay';
import {
  DOCUMENT_FORMATS,
  STRUCTURED_FORMATS,
  UPLOAD_ACCEPT,
  UPLOAD_HINT,
  acceptFor,
  isEditableFormat,
  isStructuredFormat,
} from '@/utils/documentFormats';

const STATUS_MAP: Record<ParseStatus, { label: string; color: string }> = {
  pending: { label: '待解析', color: 'default' },
  parsing: { label: '解析中', color: 'processing' },
  success: { label: '切片就绪', color: 'gold' },
  completed: { label: '已向量化', color: 'success' },
  failed: { label: '失败', color: 'error' },
  // 结构化数据空间：按设计不进解析流水线
  skipped: { label: '不参与解析', color: 'default' },
};

/** 页签键：structured 与 related 共用结构化数据空间，仅工具栏不同。 */
type TabKey = 'document' | 'structured' | 'related';

/** 三个页签对应两个文件空间：各自一棵文件夹树，文件互不混在一起。 */
const SPACE_TABS: { key: TabKey; space: FileSpace; label: string; note: string }[] = [
  {
    key: 'document',
    space: 'document',
    label: '普通文档',
    note: '上传后可预览、下载、在线编辑，并进入解析与向量化（供问答检索）。',
  },
  {
    key: 'structured',
    space: 'structured',
    label: '非关联性结构化数据',
    note: '仅显示未被文件组引用的 CSV / TSV / XLSX / JSON：自动识别字段并生成独立知识记录，可分页预览；解析完成后可点「开始向量化」写入向量，参与问答检索。',
  },
  {
    key: 'related',
    space: 'structured',
    label: '关联性结构化数据',
    note: '上传一批相互关联的结构化文件（如学生表 + 课程表 + 选课表）并新建文件组，配置主外键关系后生成关联知识记录供问答检索。',
  },
];

/** 当前页签的说明文案（Tab 下方一行，避免把功能写死在别处）。 */
const spaceNote = (tab: TabKey) => SPACE_TABS.find((s) => s.key === tab)?.note ?? '';

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

/** 允许上传的文档类型（与后端 src/core/document_formats.py 一致）。 */
const ACCEPT = UPLOAD_ACCEPT;

/**
 * 深度优先展开文件夹树（带层级），供「移动到」菜单使用。
 *
 * excludeIds 用于移动文件夹时排除自身与后代——后端也会拒绝，但提前过滤掉
 * 才能让菜单里只剩合法目标。
 */
function flattenFolders(folders: FileFolderItem[], excludeIds: string[] = []): {
  id: string;
  name: string;
  depth: number;
}[] {
  const excluded = new Set(excludeIds);
  const out: { id: string; name: string; depth: number }[] = [];
  const walk = (parentId: string | null, depth: number) => {
    const children = folders
      .filter((f) => (f.parent_id ?? null) === parentId && !excluded.has(f.id))
      .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
    children.forEach((child) => {
      out.push({ id: child.id, name: child.name, depth });
      walk(child.id, depth + 1);
    });
  };
  walk(null, 0);
  return out;
}

/** 自身 + 全部后代 id（移动文件夹时不能作为自己的子级）。 */
function collectSelfAndDescendants(folders: FileFolderItem[], rootId: string): string[] {
  const out: string[] = [rootId];
  let cursor = 0;
  while (cursor < out.length) {
    const current = out[cursor];
    cursor += 1;
    folders.forEach((f) => {
      if (f.parent_id === current) out.push(f.id);
    });
  }
  return out;
}

/** 从空间根目录到当前文件夹的路径链（面包屑数据）。 */
function folderTrail(folders: FileFolderItem[], folderId: string | null): FileFolderItem[] {
  const trail: FileFolderItem[] = [];
  let cursor: string | null = folderId;
  // 上限防脏数据成环导致死循环
  for (let i = 0; i < 100 && cursor; i += 1) {
    const found = folders.find((f) => f.id === cursor);
    if (!found) break;
    trail.unshift(found);
    cursor = found.parent_id ?? null;
  }
  return trail;
}

/** 「新建 / 重命名文件夹」弹窗状态。 */
interface FolderFormState {
  mode: 'create' | 'rename';
  folderId?: string;
  name: string;
  /** 仅 create 模式用到：落在当前目录 */
  parentId: string | null;
}

/** 「格式支持说明」弹窗表格列。 */
const FORMAT_COLUMNS: ColumnsType<(typeof DOCUMENT_FORMATS)[number]> = [
  { title: '格式', dataIndex: 'label', key: 'label', width: 70 },
  {
    title: '上传',
    key: 'upload',
    width: 60,
    align: 'center',
    render: () => <Tag color="green">支持</Tag>,
  },
  {
    title: '预览',
    key: 'preview',
    width: 60,
    align: 'center',
    render: () => <Tag color="green">支持</Tag>,
  },
  {
    title: '在线编辑',
    key: 'edit',
    width: 90,
    align: 'center',
    render: (_, row) =>
      row.editable ? <Tag color="blue">{row.editLabel}</Tag> : <Tag>不支持</Tag>,
  },
  {
    title: '下载',
    key: 'download',
    width: 60,
    align: 'center',
    render: () => <Tag color="green">支持</Tag>,
  },
  { title: '说明', dataIndex: 'note', key: 'note' },
];

export default function DocumentsPage() {
  const {
    knowledgeBases,
    selectedKbId,
    loadingKbs,
    selectKb,
    loadKnowledgeBases,
    documents,
    total,
    page,
    pageSize,
    loading,
    loadDocuments,
    removeDocument,
    reparse,
    reindex,
    error,
    clearError,
    space,
    setSpace,
    setStructuredScope,
    folders,
    loadingFolders,
    loadFolders,
    currentFolderId,
    openFolder,
  } = useDocumentStore();
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [focusDoc, setFocusDoc] = useState<DocumentItem | null>(null);
  const [previewDoc, setPreviewDoc] = useState<DocumentItem | null>(null);
  const [previewEditing, setPreviewEditing] = useState(false);
  const [formatHelpOpen, setFormatHelpOpen] = useState(false);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [replacingId, setReplacingId] = useState<string | null>(null);
  /** 正在派发「开始向量化」的文件 id（按钮 loading 用；真正进度看列表里的进度条）。 */
  const [vectorizingId, setVectorizingId] = useState<string | null>(null);
  /** 正在派发「停止向量化」的文件 id。 */
  const [stoppingId, setStoppingId] = useState<string | null>(null);
  /** 手动触发向量化后的一段时间内持续轮询，好让进度条从 0% 走到 100%。 */
  const [vectorPollUntil, setVectorPollUntil] = useState(0);
  const [folderForm, setFolderForm] = useState<FolderFormState | null>(null);
  const [folderSaving, setFolderSaving] = useState(false);
  const [renameDoc, setRenameDoc] = useState<DocumentItem | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [structuredDoc, setStructuredDoc] = useState<DocumentItem | null>(null);
  /** 关联数据（第四阶段）抽屉：新建文件组（上传一批关联文件）在抽屉内进行。 */
  const [groupsOpen, setGroupsOpen] = useState(false);
  /** 引用来源跳转（?group_id=）：抽屉打开后直接进入该文件组详情。 */
  const [initialGroupId, setInitialGroupId] = useState<string | null>(null);
  /** 文档表格批量选择（仅当前页，翻页/切换目录或空间时清空）。 */
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);
  const [batchDeleting, setBatchDeleting] = useState(false);
  /** 剪切板：Ctrl+X 记住待移动文件，Ctrl+V 移动到当前所在文件夹。 */
  const [movingIds, setMovingIds] = useState<string[]>([]);
  const [batchMoving, setBatchMoving] = useState(false);
  /** 拖拽中的文件 id 集合；当前高亮的放置目标（undefined=无，null=根目录）。 */
  const dragIdsRef = useRef<string[]>([]);
  const [dropTarget, setDropTarget] = useState<string | null | undefined>(undefined);
  /** Shift 范围选择锚点：当前页最后点击的行下标。 */
  const lastClickedIndexRef = useRef<number | null>(null);
  /** 本批次里后端回报「内容完全相同」的文件描述，批次结束时统一提示一次。 */
  const duplicatedRef = useRef<string[]>([]);

  /** 记录重复提示：判定依据是后端算的 sha256，前端不比对文件名。 */
  const noteDuplicates = (row: { file_name: string; duplicate_names?: string[] }) => {
    const names = row.duplicate_names ?? [];
    if (names.length > 0) {
      duplicatedRef.current.push(
        `「${row.file_name}」与${names.map((n) => `「${n}」`).join('、')}内容相同`,
      );
    }
  };

  /** 当前页签：structured（非关联）与 related（关联）共用结构化空间，仅工具栏不同。 */
  const [activeTab, setActiveTab] = useState<TabKey>('document');
  const isStructured = space === 'structured';
  const isRelatedTab = activeTab === 'related';

  // 初始化：加载知识库 + 当前空间文件夹树，并按 URL ?kb_id= 预选（来自「管理文档」跳转）
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const qKb = params.get('kb_id');
    const qDoc = params.get('doc_id');
    const qSpace = params.get('space');
    const qGroup = params.get('group_id');
    // 引用来源跳转（结构化 / 关联数据）需要先切到结构化空间
    if (qSpace === 'structured') {
      setSpace('structured');
      setStructuredScope(false);
      setActiveTab('structured');
    }
    if (qGroup) {
      setSpace('structured');
      setStructuredScope(true);
      setActiveTab('related');
      setInitialGroupId(qGroup);
      setGroupsOpen(true);
    }
    void loadKnowledgeBases().then(() => {
      if (qKb) {
        selectKb(qKb);
      } else {
        void loadDocuments();
      }
      if (qDoc) {
        void fetchDocument(qDoc)
          .then(setFocusDoc)
          .catch(() => {
            // 无权限或已删除：不阻断列表
          });
      }
    });
    void loadFolders();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 解析中自动轮询：任一文档 pending/parsing 时每 4s 刷新
  // 结构化文件走独立状态（parse_status 恒为 skipped），因此还要看 structured_status
  const needPoll = documents.some(
    (d) =>
      isParsing(d.parse_status) ||
      d.structured_status === 'pending' ||
      d.structured_status === 'parsing' ||
      // 手动触发的向量化正在跑：轮询刷新进度条（pending 是等用户点按钮，不轮询）
      d.structured_vector_status === 'indexing',
  ) || Date.now() < vectorPollUntil;
  useEffect(() => {
    if (!needPoll) return;
    // silent=true：轮询只更新数据，不切表格 loading，避免每 4 秒闪一次转圈
    const t = setInterval(() => void loadDocuments(undefined, undefined, true), 4_000);
    return () => clearInterval(t);
  }, [needPoll, loadDocuments]);

  // 批量选择仅对当前页有效：切换空间 / 目录 / 知识库或翻页时清空
  useEffect(() => {
    setSelectedRowKeys([]);
  }, [space, currentFolderId, selectedKbId, page, pageSize]);

  // 切换空间 / 知识库时清空剪切板（目录切换不清空：Ctrl+V 正是要粘贴到别的目录）
  useEffect(() => {
    setMovingIds([]);
  }, [space, selectedKbId]);

  // 批次上传全部完成（done/error）后，清空列表并刷新表格
  useEffect(() => {
    if (fileList.length === 0) return;
    const allDone = fileList.every((f) => f.status === 'done' || f.status === 'error');
    if (allDone) {
      const failed = fileList.filter((f) => f.status === 'error').length;
      const ok = fileList.length - failed;
      if (ok > 0) {
        message.success(
          isStructured
            ? `${ok} 个文件上传成功（后台识别字段并生成记录，不切片、不向量化）`
            : `${ok} 个文档上传成功，正在后台解析`,
        );
      }
      if (failed > 0) message.error(`${failed} 个文件上传失败`);
      // 内容重复只做提示，不自动删除、不自动覆盖：是否留用由用户决定
      if (duplicatedRef.current.length > 0) {
        message.warning(
          `检测到内容完全相同的文件（仅提示，未做任何处理）：${duplicatedRef.current.slice(0, 3).join('；')}` +
            (duplicatedRef.current.length > 3 ? ` 等 ${duplicatedRef.current.length} 组` : ''),
        );
        duplicatedRef.current = [];
      }
      const timer = setTimeout(() => {
        setFileList([]);
        void loadDocuments();
        void loadFolders();
      }, 900);
      return () => clearTimeout(timer);
    }
  }, [fileList, loadDocuments, loadFolders, isStructured]);

  // 当前目录下的子文件夹（面包屑 + 文件夹表格）
  const childFolders = useMemo(
    () =>
      folders
        .filter((f) => (f.parent_id ?? null) === (currentFolderId ?? null))
        .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN')),
    [folders, currentFolderId],
  );
  const trail = useMemo(() => folderTrail(folders, currentFolderId), [folders, currentFolderId]);

  // antd Upload 逐文件上传：走统一 API 客户端，获得与表格一致的错误处理
  const handleUpload: UploadProps['customRequest'] = async (options) => {
    const { file, onSuccess, onError, onProgress } = options;
    if (!selectedKbId) {
      onError?.(new Error('未选择知识库'));
      return;
    }
    try {
      const uploaded = await uploadDocument(file as RcFile, selectedKbId, (p) => onProgress?.({ percent: p }), {
        space,
        folderId: currentFolderId,
      });
      noteDuplicates(uploaded);
      onSuccess?.({}, file);
    } catch (err) {
      onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  };

  /** 手动重新解析结构化文件：派发后台任务后由列表轮询接管状态刷新。 */
  const handleStructuredReparse = async (row: DocumentItem) => {
    try {
      await reparseStructured(row.id);
      message.success(`已派发重新解析：${row.file_name}`);
      void loadDocuments();
    } catch (err) {
      message.error(describeApiError(err));
    }
  };

  /** 手动开始向量化：只写向量，不重新解析；进度由列表轮询刷新。 */
  const handleStructuredVectorize = async (row: DocumentItem) => {
    setVectorizingId(row.id);
    try {
      await revectorizeStructured(row.id);
      setVectorPollUntil(Date.now() + 10 * 60_000);
      message.success(`已开始向量化：${row.file_name}`);
      void loadDocuments();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setVectorizingId(null);
    }
  };

  /** 停止向量化：后端在下一批记录之间退出，清理本次部分向量后回到待向量化。 */
  const handleStructuredStop = async (row: DocumentItem) => {
    setStoppingId(row.id);
    try {
      await cancelStructuredVectorize(row.id);
      setVectorPollUntil(Date.now() + 2 * 60_000);
      message.success(`已请求停止：${row.file_name}`);
      void loadDocuments(undefined, undefined, true);
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setStoppingId(null);
    }
  };

  /** 批量删除：逐个走软删除接口，统计成功/失败，结束后清空选择并刷新列表与文件夹计数。 */
  const handleBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return;
    setBatchDeleting(true);
    let ok = 0;
    let failed = 0;
    try {
      for (const id of selectedRowKeys) {
        try {
          await deleteDocument(id);
          ok += 1;
        } catch {
          failed += 1;
        }
      }
    } finally {
      setBatchDeleting(false);
      setSelectedRowKeys([]);
      void loadDocuments();
      void loadFolders();
    }
    if (failed === 0) {
      message.success(`已删除 ${ok} ${isStructured ? '个文件' : '篇文档'}（已移入回收站）`);
    } else {
      message.warning(`删除完成：成功 ${ok} 个，失败 ${failed} 个`);
    }
  };

  /** 批量移动到目标文件夹（null=根目录）：跳过已在该位置的，逐个调移动接口并统计结果。 */
  const moveDocumentsTo = useCallback(
    async (ids: string[], targetFolderId: string | null) => {
      const movable = ids.filter((id) => {
        const doc = documents.find((d) => d.id === id);
        // 当前页找不到的（极端情况）也放行，交给后端校验
        return !doc || (doc.folder_id ?? null) !== (targetFolderId ?? null);
      });
      if (movable.length === 0) {
        message.info('所选文件已在目标位置，无需移动');
        return;
      }
      setBatchMoving(true);
      let ok = 0;
      let failed = 0;
      try {
        for (const id of movable) {
          try {
            await moveDocument(id, targetFolderId);
            ok += 1;
          } catch {
            failed += 1;
          }
        }
      } finally {
        setBatchMoving(false);
        setMovingIds([]);
        setSelectedRowKeys([]);
        void loadDocuments();
        void loadFolders();
      }
      const targetName =
        targetFolderId === null
          ? '根目录'
          : `文件夹「${folders.find((f) => f.id === targetFolderId)?.name ?? ''}」`;
      if (failed === 0) {
        message.success(`已将 ${ok} 个文件移动到${targetName}`);
      } else {
        message.warning(`移动完成：成功 ${ok} 个，失败 ${failed} 个`);
      }
    },
    [documents, folders, loadDocuments, loadFolders],
  );

  // Ctrl+X 剪切选中文件；Ctrl+V 粘贴（移动）到当前所在文件夹。输入框聚焦时不拦截。
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) {
        return;
      }
      if (!(e.ctrlKey || e.metaKey)) return;
      const key = e.key.toLowerCase();
      if (key === 'x') {
        if (selectedRowKeys.length === 0) return;
        e.preventDefault();
        setMovingIds(selectedRowKeys);
        setSelectedRowKeys([]);
        message.info(`已剪切 ${selectedRowKeys.length} 个文件，进入目标文件夹后按 Ctrl+V 粘贴`);
      } else if (key === 'v') {
        if (movingIds.length === 0) return;
        e.preventDefault();
        void moveDocumentsTo(movingIds, currentFolderId);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [selectedRowKeys, movingIds, currentFolderId, moveDocumentsTo]);

  const openPreview = (row: DocumentItem, editing = false) => {
    setPreviewEditing(editing);
    setPreviewDoc(row);
  };

  const closePreview = () => {
    setPreviewDoc(null);
    setPreviewEditing(false);
  };

  const handleDownload = async (row: DocumentItem) => {
    setDownloadingId(row.id);
    try {
      downloadBlob(await fetchDocumentContent(row.id), row.file_name);
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setDownloadingId(null);
    }
  };

  /** 替换文件：后端原子写盘并重置索引，文档 id 与知识库归属不变。 */
  const makeReplaceRequest =
    (row: DocumentItem): UploadProps['customRequest'] =>
    async (options) => {
      const { file, onSuccess, onError, onProgress } = options;
      setReplacingId(row.id);
      try {
        const result = await replaceDocument(row.id, file as RcFile, (p) =>
          onProgress?.({ percent: p }),
        );
        message.success(result.message || '替换成功');
        onSuccess?.({}, file);
        void loadDocuments();
      } catch (err) {
        message.error(describeApiError(err));
        onError?.(err instanceof Error ? err : new Error(String(err)));
      } finally {
        setReplacingId(null);
      }
    };

  // ── 文件夹动作 ────────────────────────────────────────────────────────────

  const submitFolderForm = async () => {
    if (!folderForm) return;
    const name = folderForm.name.trim();
    if (!name) {
      message.warning('请输入文件夹名称');
      return;
    }
    setFolderSaving(true);
    try {
      if (folderForm.mode === 'create') {
        await createFolder({ name, space, parent_id: folderForm.parentId });
        message.success(`文件夹「${name}」已创建`);
      } else if (folderForm.folderId) {
        // 只改名：不传 parent_id，父级保持不变
        await updateFolder(folderForm.folderId, { name });
        message.success(`文件夹已重命名为「${name}」`);
      }
      setFolderForm(null);
      await loadFolders();
      void loadDocuments();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setFolderSaving(false);
    }
  };

  const handleFolderMove = async (folder: FileFolderItem, targetId: string | null) => {
    if ((folder.parent_id ?? null) === (targetId ?? null)) return;
    try {
      await updateFolder(folder.id, { parent_id: targetId });
      message.success(
        targetId === null
          ? `文件夹「${folder.name}」已移动到根目录`
          : `文件夹「${folder.name}」已移动`,
      );
      await loadFolders();
      void loadDocuments();
    } catch (err) {
      message.error(describeApiError(err));
    }
  };

  const handleFolderDelete = async (folder: FileFolderItem) => {
    try {
      const result = await deleteFolder(folder.id);
      message.success(result.message || '文件夹已删除');
      // 删除的正是当前所在目录时退回上级
      if (currentFolderId === folder.id) openFolder(folder.parent_id ?? null);
      else await loadFolders();
    } catch (err) {
      message.error(describeApiError(err));
    }
  };

  /** 移动文件到指定文件夹：只改 folder_id，物理文件与索引不动。 */
  const handleDocumentMove = async (row: DocumentItem, targetId: string | null) => {
    if ((row.folder_id ?? null) === (targetId ?? null)) return;
    try {
      await moveDocument(row.id, targetId);
      message.success(targetId === null ? `已移动到根目录` : `已移动到目标文件夹`);
      await loadFolders();
      void loadDocuments();
    } catch (err) {
      message.error(describeApiError(err));
    }
  };

  const submitRename = async () => {
    if (!renameDoc) return;
    const name = renameValue.trim();
    if (!name) {
      message.warning('请输入文件名');
      return;
    }
    setRenaming(true);
    try {
      await renameDocument(renameDoc.id, name);
      message.success('重命名成功');
      setRenameDoc(null);
      void loadDocuments();
    } catch (err) {
      message.error(describeApiError(err));
    } finally {
      setRenaming(false);
    }
  };

  /** 生成「移动到」菜单项：根目录 + 该空间文件夹树（可按需排除不可选目标）。 */
  const moveMenuItems = (excludeIds: string[], onPick: (targetId: string | null) => void) => [
    {
      key: '__root__',
      label: <span style={{ display: 'block', fontWeight: 500 }}>空间根目录</span>,
      onClick: () => onPick(null),
    },
    ...flattenFolders(folders, excludeIds).map((f) => ({
      key: f.id,
      label: (
        <span style={{ display: 'block', paddingLeft: f.depth * 16 }}>
          <FolderOutlined /> {f.name}
        </span>
      ),
      onClick: () => onPick(f.id),
    })),
  ];

  // ── 表格列 ────────────────────────────────────────────────────────────────

  const folderColumns: ColumnsType<FileFolderItem> = [
    {
      title: '文件夹',
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
      render: (name: string, row) => (
        <a onClick={() => openFolder(row.id)}>
          <FolderOutlined style={{ marginRight: 6 }} />
          {name}
        </a>
      ),
    },
    {
      title: '文件数',
      dataIndex: 'document_count',
      key: 'document_count',
      width: 90,
      render: (n: number) => (n > 0 ? n : <Typography.Text type="secondary">空</Typography.Text>),
    },
    { title: '子文件夹', dataIndex: 'child_count', key: 'child_count', width: 90 },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 170,
      render: (t: string) => formatTime(t),
    },
    {
      title: '操作',
      key: 'action',
      width: 250,
      render: (_, row) => {
        const isEmpty = row.document_count === 0 && row.child_count === 0;
        return (
          <Space size={4} wrap>
            <Button size="small" icon={<FolderOpenOutlined />} onClick={() => openFolder(row.id)}>
              进入
            </Button>
            <Button
              size="small"
              icon={<EditOutlined />}
              onClick={() =>
                setFolderForm({ mode: 'rename', folderId: row.id, name: row.name, parentId: null })
              }
            >
              重命名
            </Button>
            <Dropdown
              menu={{
                items: moveMenuItems(
                  collectSelfAndDescendants(folders, row.id),
                  (targetId) => void handleFolderMove(row, targetId),
                ),
              }}
            >
              <Tooltip title="移动整个文件夹（子文件夹一起跟随，物理文件不动）">
                <Button size="small" icon={<ExportOutlined />}>
                  移动到
                </Button>
              </Tooltip>
            </Dropdown>
            {isEmpty ? (
              <Popconfirm
                title="确认删除该文件夹？"
                description="文件夹为空，删除后不影响任何文件。"
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                onConfirm={() => void handleFolderDelete(row)}
              >
                <Button size="small" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            ) : (
              <Tooltip
                title={`文件夹非空（${row.child_count} 个子文件夹、${row.document_count} 个文件），请先移出内容再删除`}
              >
                <Button size="small" danger icon={<DeleteOutlined />} disabled />
              </Tooltip>
            )}
          </Space>
        );
      },
    },
  ];

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
      title: '大小',
      dataIndex: 'file_size',
      key: 'file_size',
      width: 100,
      render: (s: number) => formatBytes(s),
    },
    {
      title: '处理状态',
      dataIndex: isStructured ? 'structured_status' : 'parse_status',
      key: isStructured ? 'structured_status' : 'parse_status',
      width: 190,
      render: (_: unknown, row) => {
        // 统一状态词汇（待处理/处理中/待用户确认/已完成/处理失败）+ 本业务的细粒度说明。
        // 阶段链与完整失败原因在预览抽屉的「处理概览」里看（那里是后端真实派生结果）。
        const detail = isStructured
          ? isStructuredFormat(row.file_type)
            ? structuredParseStatusMeta(row.structured_status ?? 'pending').label
            : '该格式不参与结构化解析'
          : (STATUS_MAP[row.parse_status]?.label ?? row.parse_status);
        const unified = isStructured
          ? structuredRowStatus(row)
          : documentRowStatus(row);
        const counts = isStructured
          ? row.structured_status === 'success'
            ? `${row.structured_field_count ?? 0} 字段 / ${row.structured_record_count ?? 0} 条记录`
            : ''
          : row.chunk_count
            ? `切片 ${row.chunk_count} / 向量 ${row.vector_count ?? 0}`
            : '';
        const reason = isStructured
          ? row.structured_error_message || ''
          : row.error_message || '';
        // 向量化进度：只有解析成功且有记录、且尚未完成时显示（0% 表示等待手动触发）
        const records = row.structured_record_count ?? 0;
        const showVectorProgress =
          isStructured &&
          row.structured_status === 'success' &&
          records > 0 &&
          row.structured_vector_status !== 'completed' &&
          row.structured_vector_status !== null &&
          row.structured_vector_status !== undefined;
        const vectorPercent = showVectorProgress
          ? Math.min(100, Math.round(((row.structured_vector_count ?? 0) / records) * 100))
          : 0;
        return (
          <div>
            <Space size={6}>
              <ProcessingStatusTag status={unified} detail={detail} />
              {reason ? (
                <Tooltip title={reason}>
                  <Typography.Text type="danger" style={{ maxWidth: 96 }} ellipsis>
                    {reason}
                  </Typography.Text>
                </Tooltip>
              ) : counts ? (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {counts}
                </Typography.Text>
              ) : null}
            </Space>
            {showVectorProgress && (
              <Tooltip
                title={`向量 ${row.structured_vector_count ?? 0} / ${records} 条${
                  row.structured_vector_error ? `：${row.structured_vector_error}` : ''
                }`}
              >
                <Progress
                  percent={vectorPercent}
                  size="small"
                  status={row.structured_vector_status === 'failed' ? 'exception' : undefined}
                  style={{ width: 150, margin: '4px 0 0' }}
                />
              </Tooltip>
            )}
          </div>
        );
      },
    },
    {
      title: '切片数',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 90,
      render: (n: number) => (isStructured ? <Typography.Text type="secondary">-</Typography.Text> : n),
    },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (t: string) => formatTime(t),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 170,
      render: (t?: string) => (t ? formatTime(t) : '-'),
    },
    {
      title: '操作',
      key: 'action',
      width: 360,
      render: (_, row) => {
        const busy = isParsing(row.parse_status);
        return (
          <Space size={4} wrap>
            <Tooltip
              title={
                isEditableFormat(row.file_type)
                  ? '预览内容，支持在线编辑'
                  : '在当前页面预览（该格式不支持在线编辑）'
              }
            >
              <Button size="small" icon={<EyeOutlined />} onClick={() => openPreview(row)}>
                打开
              </Button>
            </Tooltip>
            {isEditableFormat(row.file_type) && (
              <Tooltip title={busy ? '解析进行中，稍后可编辑' : '在线编辑并保存'}>
                <Button
                  size="small"
                  icon={<EditOutlined />}
                  disabled={busy}
                  onClick={() => openPreview(row, true)}
                >
                  编辑
                </Button>
              </Tooltip>
            )}
            <Tooltip title="下载原始文件">
              <Button
                size="small"
                icon={<DownloadOutlined />}
                loading={downloadingId === row.id}
                onClick={() => void handleDownload(row)}
              >
                下载
              </Button>
            </Tooltip>
            {!isStructured && (
              <Tooltip
                title={
                  isParsing(row.parse_status)
                    ? '解析进行中，完成后可向量化'
                    : row.parse_status === 'failed'
                      ? '解析失败，请先重新解析'
                      : row.index_status === 'indexed'
                        ? '按当前切片重建向量（先清旧向量再重写）'
                        : '开始向量化，把已切片内容写入 Milvus 供问答检索'
                }
              >
                <Button
                  size="small"
                  type={row.index_status === 'indexed' ? 'default' : 'primary'}
                  ghost
                  icon={<ThunderboltOutlined />}
                  disabled={
                    isParsing(row.parse_status) ||
                    row.parse_status === 'failed' ||
                    (row.chunk_count ?? 0) === 0
                  }
                  onClick={() => void reindex(row.id)}
                >
                  {isParsing(row.parse_status)
                    ? '解析中'
                    : row.parse_status === 'failed'
                      ? '解析失败'
                      : row.index_status === 'indexed'
                        ? '重新向量化'
                        : '开始向量化'}
                </Button>
              </Tooltip>
            )}
            {isStructured && isStructuredFormat(row.file_type) && (
              <Tooltip title="查看识别到的字段、分页浏览知识记录并追溯来源">
                <Button
                  size="small"
                  icon={<TableOutlined />}
                  onClick={() => setStructuredDoc(row)}
                >
                  数据预览
                </Button>
              </Tooltip>
            )}
            {isStructured && isStructuredFormat(row.file_type) && (
              <Tooltip
                title={
                  row.structured_status === 'parsing' || row.structured_status === 'pending'
                    ? '解析完成后可向量化'
                    : row.structured_status === 'failed'
                      ? '解析失败，请先重新解析'
                      : (row.structured_record_count ?? 0) === 0
                        ? '暂无记录，请先重新解析生成记录'
                        : row.structured_vector_status === 'completed'
                          ? '按当前知识记录重建向量（不重新解析、不产生重复向量）'
                          : '开始为该文件的每条知识记录写入向量（供问答检索）'
                }
              >
                <Button
                  size="small"
                  type={
                    row.structured_vector_status === 'pending' ||
                    (row.structured_vector_status == null &&
                      row.structured_status === 'success' &&
                      (row.structured_record_count ?? 0) > 0)
                      ? 'primary'
                      : 'default'
                  }
                  ghost
                  icon={<ThunderboltOutlined />}
                  loading={vectorizingId === row.id}
                  disabled={
                    row.structured_vector_status === 'indexing' ||
                    row.structured_status === 'parsing' ||
                    row.structured_status === 'pending' ||
                    row.structured_status === 'failed' ||
                    (row.structured_record_count ?? 0) === 0
                  }
                  onClick={() => void handleStructuredVectorize(row)}
                >
                  {row.structured_vector_status === 'indexing'
                    ? '向量化中'
                    : row.structured_status === 'parsing' || row.structured_status === 'pending'
                      ? '待解析'
                      : row.structured_status === 'failed'
                        ? '解析失败'
                        : (row.structured_record_count ?? 0) === 0
                          ? '无记录'
                          : row.structured_vector_status === 'completed'
                            ? '重新向量化'
                            : (row.structured_vector_count ?? 0) > 0
                              ? '继续向量化'
                              : '开始向量化'}
                </Button>
              </Tooltip>
            )}
            {isStructured && row.structured_vector_status === 'indexing' && (
              <Tooltip title="停止本次向量化（后端在下一批记录之间退出，已写入的部分保留，下次继续）">
                <Button
                  size="small"
                  danger
                  ghost
                  icon={<StopOutlined />}
                  loading={stoppingId === row.id}
                  onClick={() => void handleStructuredStop(row)}
                >
                  停止
                </Button>
              </Tooltip>
            )}
            <Tooltip
              title={
                busy
                  ? '解析进行中，请稍后替换'
                  : `上传同名格式文件替换当前文档（仅接受 .${row.file_type}），文档 ID 不变${
                      isStructured ? '' : '并自动重建检索索引'
                    }`
              }
            >
              <Upload
                accept={acceptFor(row.file_type)}
                showUploadList={false}
                disabled={busy || replacingId !== null}
                customRequest={makeReplaceRequest(row)}
              >
                <Button
                  size="small"
                  icon={<SwapOutlined />}
                  loading={replacingId === row.id}
                  disabled={busy}
                >
                  替换文件
                </Button>
              </Upload>
            </Tooltip>
            <Dropdown
              menu={{
                items: [
                  {
                    key: 'rename',
                    icon: <EditOutlined />,
                    label: '重命名',
                    onClick: () => {
                      setRenameDoc(row);
                      setRenameValue(row.file_name);
                    },
                  },
                  {
                    key: 'move',
                    icon: <ExportOutlined />,
                    label: '移动到…',
                    children: moveMenuItems([], (targetId) =>
                      void handleDocumentMove(row, targetId),
                    ),
                  },
                  ...(isStructured
                    ? isStructuredFormat(row.file_type)
                      ? [
                          {
                            key: 'structured-reparse',
                            icon: <ReloadOutlined />,
                            label:
                              row.structured_status === 'parsing' || row.structured_status === 'pending'
                                ? '解析进行中'
                                : '重新解析（重建记录）',
                            disabled:
                              row.structured_status === 'parsing' ||
                              row.structured_status === 'pending',
                            onClick: () => void handleStructuredReparse(row),
                          },
                        ]
                      : []
                    : [
                        {
                          key: 'reparse',
                          icon: <ReloadOutlined />,
                          label: busy ? '解析进行中' : '重新解析',
                          disabled: busy,
                          onClick: () => void reparse(row.id),
                        },
                      ]),
                ],
              }}
            >
              <Tooltip title="重命名 / 移动 / 重新解析">
                <Button size="small" icon={<EllipsisOutlined />} />
              </Tooltip>
            </Dropdown>
            <Popconfirm
              title="确认删除该文档？"
              description={
                isStructured
                  ? '删除该文件及其已生成的结构化知识记录，不影响其他文件与文件夹。'
                  : '将同时删除其切片与向量数据，不可恢复。'
              }
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
      <Button icon={<FileTextOutlined />} onClick={() => setFormatHelpOpen(true)}>
        格式支持说明
      </Button>
      {!isRelatedTab && (
        <Upload
          fileList={fileList}
          accept={ACCEPT}
          showUploadList
          disabled={!selectedKbId}
          multiple
          customRequest={handleUpload}
          onChange={({ fileList: fl }) => setFileList(fl)}
        >
          <Tooltip
            title={
              !selectedKbId
                ? '请先选择知识库'
                : `支持 ${UPLOAD_HINT}，另支持常见图片格式，可多选${
                    currentFolderId ? '，将上传到当前文件夹' : ''
                  }`
            }
          >
            <Button type="primary" icon={<CloudUploadOutlined />} disabled={!selectedKbId}>
              {isStructured ? '上传非关联性数据' : '上传文档'}
            </Button>
          </Tooltip>
        </Upload>
      )}
      {isRelatedTab && (
        <Tooltip
          title={
            !selectedKbId
              ? '请先选择知识库'
              : '查看已有文件组：结构识别、关系确认、数据校验与关联知识记录；也可在抽屉内一次多选同批关联文件（如学生表 + 课程表 + 选课表），上传后新建文件组'
          }
        >
          <Button
            type="primary"
            icon={<CloudUploadOutlined />}
            disabled={!selectedKbId}
            onClick={() => setGroupsOpen(true)}
          >
            上传关联数据
          </Button>
        </Tooltip>
      )}
      <AdminHeaderRight />
    </Space>
  );

  /** 面包屑项兼作拖拽放置目标：把文件拖到某级目录即移动到该目录。 */
  const wrapDropTarget = (node: ReactNode, folderId: string | null) => (
    <span
      onDragOver={(e) => {
        if (dragIdsRef.current.length === 0) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        setDropTarget(folderId);
      }}
      onDragLeave={() => setDropTarget((prev) => (prev === folderId ? undefined : prev))}
      onDrop={(e) => {
        e.preventDefault();
        const ids = dragIdsRef.current;
        dragIdsRef.current = [];
        setDropTarget(undefined);
        if (ids.length > 0) void moveDocumentsTo(ids, folderId);
      }}
      style={
        dropTarget === folderId
          ? {
              background: '#e6f4ff',
              outline: '1px dashed #1677ff',
              borderRadius: 4,
              padding: '0 4px',
            }
          : undefined
      }
    >
      {node}
    </span>
  );

  const breadcrumbItems = [
    {
      title: wrapDropTarget(
        <a onClick={() => openFolder(null)}>
          {isStructured ? '结构化数据根目录' : '普通文档根目录'}
        </a>,
        null,
      ),
    },
    ...trail.map((f) => ({
      title: wrapDropTarget(
        <a onClick={() => openFolder(f.id)}>
          <FolderOutlined style={{ marginRight: 4 }} />
          {f.name}
        </a>,
        f.id,
      ),
    })),
  ];

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      <Tabs
        activeKey={activeTab}
        onChange={(key) => {
          const tab = key as TabKey;
          setActiveTab(tab);
          const target = SPACE_TABS.find((s) => s.key === tab)?.space ?? 'document';
          // 非关联/关联两个页签共用 structured 空间，但展示范围互斥：
          // 非关联页签只看未被文件组引用的文件，关联页签只看文件组成员
          if (target === 'structured') setStructuredScope(tab === 'related');
          else if (target !== space) setSpace(target);
        }}
        items={SPACE_TABS.map((s) => ({ key: s.key, label: s.label }))}
      />

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

      {focusDoc && (
        <Alert
          type="info"
          showIcon
          closable
          style={{ marginBottom: 12 }}
          message={`已定位到引用来源文档：${focusDoc.file_name}`}
          description="可在下方表格中重新解析、重新向量化或删除该文档。"
          onClose={() => setFocusDoc(null)}
          action={
            <Button
              danger
              size="small"
              onClick={async () => {
                await removeDocument(focusDoc.id);
                setFocusDoc(null);
              }}
            >
              删除文档
            </Button>
          }
        />
      )}

      <Space style={{ marginBottom: 8 }} align="center" wrap>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {spaceNote(activeTab)}
        </Typography.Text>
      </Space>

      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }} wrap>
        <Breadcrumb items={breadcrumbItems} />
        <Space size={8}>
          {movingIds.length > 0 && (
            <Tag closable color="processing" onClose={() => setMovingIds([])}>
              已剪切 {movingIds.length} 个文件，Ctrl+V 粘贴到当前目录
            </Tag>
          )}
          <Button
            icon={<ArrowLeftOutlined />}
            disabled={currentFolderId === null}
            onClick={() => openFolder(trail.length > 1 ? trail[trail.length - 2].id : null)}
          >
            返回上级
          </Button>
          <Button
            icon={<FolderAddOutlined />}
            onClick={() =>
              setFolderForm({ mode: 'create', name: '', parentId: currentFolderId })
            }
          >
            新建文件夹
          </Button>
          <Popconfirm
            title={`确认删除选中的 ${selectedRowKeys.length} ${isStructured ? '个文件' : '篇文档'}？`}
            description={
              isStructured
                ? '将删除这些文件及其已生成的结构化知识记录，不影响文件夹。'
                : '将同时删除其切片与向量数据，不可恢复。'
            }
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => void handleBatchDelete()}
          >
            <Button
              danger
              icon={<DeleteOutlined />}
              disabled={selectedRowKeys.length === 0}
              loading={batchDeleting}
            >
              批量删除{selectedRowKeys.length > 0 ? `（${selectedRowKeys.length}）` : ''}
            </Button>
          </Popconfirm>
        </Space>
      </Space>

      {(childFolders.length > 0 || loadingFolders) && (
        <Table
          rowKey="id"
          size="small"
          style={{ marginBottom: 16 }}
          columns={folderColumns}
          dataSource={childFolders}
          loading={loadingFolders}
          pagination={false}
          onRow={(record) => ({
            // 文件夹行是拖拽放置目标：把文件（可多选批量）拖进来即移动
            onDragOver: (e) => {
              if (dragIdsRef.current.length === 0) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = 'move';
              setDropTarget(record.id);
            },
            onDragLeave: () =>
              setDropTarget((prev) => (prev === record.id ? undefined : prev)),
            onDrop: (e) => {
              e.preventDefault();
              const ids = dragIdsRef.current;
              dragIdsRef.current = [];
              setDropTarget(undefined);
              if (ids.length > 0) void moveDocumentsTo(ids, record.id);
            },
            style:
              dropTarget === record.id
                ? { background: '#e6f4ff', outline: '1px dashed #1677ff' }
                : undefined,
          })}
        />
      )}

      {currentFolderId && childFolders.length === 0 && !loadingFolders && (
        <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
          当前文件夹内没有子文件夹；目录内文件不按知识库筛选，上传将归入所选知识库。
        </Typography.Text>
      )}

      <Table
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={documents}
        loading={loading}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys as string[]),
        }}
        onRow={(record, index) => ({
          // 行可拖拽：拖已选中的行=批量拖动整个选择，否则只拖当前行
          draggable: true,
          style: movingIds.includes(record.id) ? { opacity: 0.45 } : undefined,
          onClick: (e) => {
            if (!e.shiftKey) {
              lastClickedIndexRef.current = index ?? null;
              return;
            }
            // Shift+点击：以最后一次点击为锚点，把当前页范围内所有行并入选择
            e.preventDefault();
            const anchor = lastClickedIndexRef.current;
            if (anchor == null || index == null || anchor === index) return;
            const [from, to] = anchor < index ? [anchor, index] : [index, anchor];
            const rangeKeys = documents.slice(from, to + 1).map((d) => d.id);
            setSelectedRowKeys((prev) => Array.from(new Set([...prev, ...rangeKeys])));
            lastClickedIndexRef.current = index;
          },
          onDragStart: (e) => {
            dragIdsRef.current = selectedRowKeys.includes(record.id)
              ? selectedRowKeys
              : [record.id];
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', dragIdsRef.current.join(','));
          },
          onDragEnd: () => {
            dragIdsRef.current = [];
            setDropTarget(undefined);
          },
        })}
        rowClassName={(record) =>
          focusDoc && record.id === focusDoc.id ? 'source-focus-row' : ''
        }
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showTotal: (n) => `共 ${n} ${isStructured ? '个文件' : '篇'}`,
          onChange: (nextPage, nextSize) => void loadDocuments(nextPage, nextSize),
        }}
        locale={{
          emptyText: currentFolderId
            ? '该文件夹暂无文件'
            : selectedKbId
              ? '该知识库暂无文件'
              : '请选择知识库查看文件',
        }}
      />

      <DocumentPreviewDrawer
        doc={previewDoc}
        open={previewDoc !== null}
        initialEditing={previewEditing}
        onClose={closePreview}
        onSaved={() => void loadDocuments()}
      />

      <StructuredPreviewDrawer
        doc={structuredDoc}
        open={structuredDoc !== null}
        onClose={() => setStructuredDoc(null)}
        onChanged={() => void loadDocuments()}
      />

      <StructuredGroupsDrawer
        kbId={selectedKbId}
        folderId={currentFolderId}
        open={groupsOpen}
        initialGroupId={initialGroupId}
        onClose={() => setGroupsOpen(false)}
        onChanged={() => void loadDocuments()}
      />

      <Modal
        title={folderForm?.mode === 'rename' ? '重命名文件夹' : '新建文件夹'}
        open={folderForm !== null}
        confirmLoading={folderSaving}
        okText="确定"
        cancelText="取消"
        onOk={() => void submitFolderForm()}
        onCancel={() => setFolderForm(null)}
      >
        <Input
          autoFocus
          maxLength={64}
          placeholder="文件夹名称（同一层级下不可重名）"
          value={folderForm?.name ?? ''}
          onChange={(e) =>
            setFolderForm((prev) => (prev ? { ...prev, name: e.target.value } : prev))
          }
          onPressEnter={() => void submitFolderForm()}
        />
        {folderForm?.mode === 'create' && (
          <p style={{ margin: '8px 0 0', color: '#8c8c8c', fontSize: 12 }}>
            将创建在：
            {trail.length ? trail.map((f) => f.name).join(' / ') : '当前空间根目录'}
          </p>
        )}
      </Modal>

      <Modal
        title="重命名文件"
        open={renameDoc !== null}
        confirmLoading={renaming}
        okText="保存"
        cancelText="取消"
        onOk={() => void submitRename()}
        onCancel={() => setRenameDoc(null)}
      >
        <Input
          autoFocus
          maxLength={255}
          value={renameValue}
          onChange={(e) => setRenameValue(e.target.value)}
          onPressEnter={() => void submitRename()}
        />
        <p style={{ margin: '8px 0 0', color: '#8c8c8c', fontSize: 12 }}>
          只修改展示文件名，扩展名需与上传格式一致（改为其他扩展名会被拒绝）。物理文件与文档
          ID 不变。
        </p>
      </Modal>

      <Modal
        title="格式支持说明"
        open={formatHelpOpen}
        onCancel={() => setFormatHelpOpen(false)}
        footer={null}
        width={860}
      >
        <Table
          rowKey="ext"
          size="small"
          pagination={false}
          columns={FORMAT_COLUMNS}
          dataSource={DOCUMENT_FORMATS}
        />
        <p style={{ margin: '12px 0 0', color: '#8c8c8c', fontSize: 12 }}>
          所有格式均可上传、预览和下载；「在线编辑」保存后会自动重新解析并向量化。
          不可在线编辑的格式请使用「替换文件」：下载原文件 → 本地修改 → 上传同格式文件，
          文档 ID 与知识库归属保持不变。常见图片格式（PNG/JPG/GIF/WebP/BMP）仅支持预览与下载。
        </p>
        {isStructured && (
          <p style={{ margin: '8px 0 0', color: '#8c8c8c', fontSize: 12 }}>
            结构化数据空间的 {STRUCTURED_FORMATS.map((e) => e.toUpperCase()).join(' / ')}{' '}
            会识别字段并生成独立知识记录（可分页预览与来源追溯），但不切片、不做
            Embedding、不写向量库；保存与替换只更新文件本身并重新识别字段，解析失败时保留原有记录。
            其他格式仅支持上传、预览、下载与替换。
          </p>
        )}
      </Modal>
    </AppLayout>
  );
}
