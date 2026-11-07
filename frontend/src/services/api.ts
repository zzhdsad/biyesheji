import axios from 'axios';
import type {
  AuditLog,
  ChatMessage,
  Citation,
  Conversation,
  DocumentItem,
  DocumentPreview,
  DocumentProcessingInfo,
  GroupProcessingInfo,
  SaveDocumentResult,
  StructuredParseInfo,
  StructuredRecordDetail,
  StructuredRecordPage,
  StructuredGroup,
  StructuredGroupDetail,
  StructuredGroupRecordDetail,
  StructuredGroupRecordPage,
  StructuredGroupValidation,
  StructuredRelation,
  EvidenceGroup,
  EvidenceSummary,
  FileFolderItem,
  FileSpace,
  QueryAnalysis,
  RouterDecision,
  GateDecision,
  HealthResponse,
  ReflectionDecision,
  ReplaceDocumentResult,
  KBMember,
  KBMemberRole,
  KnowledgeBase,
  MessageOut,
  Role,
  SystemConfig,
  SystemConfigValues,
  UserOut,
} from '@/types';
import { getToken, handleUnauthorized } from './token';

/** 统一 API 客户端：开发环境经 Next.js rewrites 代理到 FastAPI。 */
export const api = axios.create({
  baseURL: '/api/v1',
  timeout: 60_000, // RAG 检索+生成可能较慢，放宽超时
});

// ── 拦截器：自动携带 JWT + 401 自动跳登录 ───────────────────────────────────
// 请求拦截器：每个请求自动注入 Authorization: Bearer <token>
api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// 响应拦截器：401（未鉴权/token 过期）→ 清 token + 硬跳登录页
// 与 SSE（hooks/useSSE.ts）共用 token.ts 的 handleUnauthorized（BUG-062）
api.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err?.response?.status;
    if (status === 401) {
      // /login 自身的 401 不跳转（避免登录页请求失败时反复跳转）
      // 无 /register：注册入口已下线（BUG-014），账号由管理员创建
      handleUnauthorized();
    }
    return Promise.reject(err);
  },
);

export interface ChatAnswer {
  conversation_id: string;
  message_id: string;
  answer: string;
  citations: Citation[];
  /** 阶段十：统一 Evidence 与多来源分组（非流式 /ask） */
  evidence?: Citation[];
  evidence_groups?: EvidenceGroup[];
  evidence_summary?: EvidenceSummary;
  /** 阶段十一：Query 分析结果 */
  query_analysis?: QueryAnalysis;
  /** 阶段十二：Dynamic Router 决策 */
  router_decision?: RouterDecision;
  /** 阶段十三：KG 关系证据切片（citations/evidence 已包含，此处为可选视图） */
  kg_evidence?: Citation[];
  /** 阶段十四：Evidence Gate 决策（Gate 关闭时为 null） */
  evidence_gate?: GateDecision | null;
  /** 阶段十五：Self Reflection 决策（Reflection 关闭时为 null） */
  reflection?: ReflectionDecision | null;
}

/** RAG 问答：向量检索 → Rerank → LLM 生成 → 引用溯源。 */
export async function askQuestion(payload: {
  question: string;
  kb_ids?: string[];
  conversation_id?: string | null;
}): Promise<ChatAnswer> {
  const { data } = await api.post<ChatAnswer>('/chat/ask', payload);
  return data;
}

/**
 * 证据原文 → AI 中文翻译（POST /chat/evidence/translate）。
 *
 * 纯展示辅助接口：复用现有 LLM 配置，不触碰检索链路；后端带缓存，
 * 同一段原文重复请求直接命中。原文已为中文时返回 is_chinese=true 且
 * 不调用模型。
 */
export async function translateEvidence(text: string): Promise<{
  translation: string | null;
  is_chinese: boolean;
  cached?: boolean;
}> {
  const { data } = await api.post<{
    translation: string | null;
    is_chinese: boolean;
    cached?: boolean;
  }>('/chat/evidence/translate', { text });
  return data;
}

/** 知识库列表（侧边栏数据源）。 */
export async function fetchKnowledgeBases(): Promise<KnowledgeBase[]> {
  const { data } = await api.get<KnowledgeBase[]>('/kb');
  return data;
}

/** 创建知识库（POST /kb，owner_id 由后端从当前登录用户取）。 */
export async function createKb(payload: {
  name: string;
  description?: string;
  visibility?: 'public' | 'private';
}): Promise<KnowledgeBase> {
  const { data } = await api.post<KnowledgeBase>('/kb', payload);
  return data;
}

/** 编辑知识库（PUT /kb/{id}，partial update；仅 owner 或 admin 可调用）。 */
export async function updateKb(
  kbId: string,
  payload: Partial<Pick<KnowledgeBase, 'name' | 'description' | 'visibility'>>,
): Promise<KnowledgeBase> {
  const { data } = await api.put<KnowledgeBase>(`/kb/${kbId}`, payload);
  return data;
}

/** 删除知识库（DELETE /kb/{id}，documents 外键 CASCADE 级联删除）。 */
export async function deleteKb(kbId: string): Promise<void> {
  await api.delete(`/kb/${kbId}`);
}

/** 历史会话列表（按创建时间倒序）。 */
export async function fetchConversations(): Promise<Conversation[]> {
  const { data } = await api.get<Conversation[]>('/chat/conversations');
  return data;
}

/** 会话消息记录（按时间升序，解包 citations.sources）。 */
export async function fetchMessages(conversationId: string): Promise<ChatMessage[]> {
  const { data } = await api.get<MessageOut[]>(
    `/chat/conversations/${conversationId}/messages`,
  );
  return data.map((m) => ({
    id: m.id,
    role: m.role as Role,
    content: m.content,
    citations: m.citations?.sources ?? [],
  }));
}

/** 删除某条用户提问及其后的消息（编辑后重新提问前调用）。 */
export async function deleteMessagesFrom(
  conversationId: string,
  messageId: string,
): Promise<{ deleted_count: number }> {
  const { data } = await api.delete<{ deleted_count: number }>(
    `/chat/conversations/${conversationId}/messages/from/${messageId}`,
  );
  return data;
}

/** 删除历史会话（DELETE /chat/conversations/{id}，级联删除消息 + 清缓存）。 */
export async function deleteConversation(conversationId: string): Promise<void> {
  await api.delete(`/chat/conversations/${conversationId}`);
}

/** 删除当前用户的全部历史会话（DELETE /chat/conversations）。 */
export async function deleteAllConversations(): Promise<{ deletedCount: number }> {
  const { data } = await api.delete<{ deleted_count: number }>('/chat/conversations');
  return { deletedCount: data.deleted_count };
}

/** 系统健康状态（postgres/redis/milvus）。 */
export async function fetchHealth(): Promise<HealthResponse> {
  const { data } = await axios.get<HealthResponse>('/health', { timeout: 5_000 });
  return data;
}

// ─────────────────────────── 文档管理 ───────────────────────────

/** 文档列表，支持按知识库与解析状态筛选。 */
export async function fetchDocuments(kbId?: string): Promise<DocumentItem[]> {
  const { data } = await api.get<DocumentItem[]>('/documents', {
    params: kbId ? { kb_id: kbId } : {},
  });
  return data;
}

/** 单个文档详情（问答引用“定位来源”使用）。 */
export async function fetchDocument(docId: string): Promise<DocumentItem> {
  const { data } = await api.get<DocumentItem>(`/documents/${docId}`);
  return data;
}

/** 文档原始内容（带鉴权请求，前端取 Blob 在弹窗内预览或下载）。 */
export async function fetchDocumentContent(docId: string): Promise<Blob> {
  const { data } = await api.get<Blob>(`/documents/${docId}/content`, {
    responseType: 'blob',
    timeout: 120_000,
  });
  return data;
}

/** 站内预览数据：文本/表格结构化内容或 Office 抽取结果。 */
export async function fetchDocumentPreview(docId: string): Promise<DocumentPreview> {
  const { data } = await api.get<DocumentPreview>(`/documents/${docId}/preview`, {
    timeout: 120_000,
  });
  return data;
}

/** 保存在线编辑内容（MD/TXT/CSV/TSV/JSON/HTML），后端会重新解析并更新向量索引。 */
export async function saveDocumentContent(
  docId: string,
  content: string,
  encoding?: string,
): Promise<SaveDocumentResult> {
  const { data } = await api.post<SaveDocumentResult>(`/documents/${docId}/content`, {
    content,
    ...(encoding ? { encoding } : {}),
  });
  return data;
}

/**
 * 替换文档原始文件（POST /documents/{id}/replace）。
 *
 * 后端复用同一 storage_path 原子写盘并重置索引状态，文档 id 与知识库归属不变；
 * 格式必须与原文档一致，校验或写入失败时原文件保持不变。
 */
export async function replaceDocument(
  docId: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<ReplaceDocumentResult> {
  const form = new FormData();
  form.append('file', file);
  const { data } = await api.post<ReplaceDocumentResult>(`/documents/${docId}/replace`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: (e) => {
      if (e.total && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
    },
    timeout: 120_000, // 大文件上传放宽超时
  });
  return data;
}

export interface DocumentPage {
  items: DocumentItem[];
  total: number;
}

/** 文档列表筛选参数（第一阶段文件管理：space / 目录）。 */
export interface DocumentListParams {
  kbId?: string;
  /** 文件空间，默认 document；不传时后端也只返回普通文档 */
  space?: FileSpace;
  /** 只看该文件夹内的文件 */
  folderId?: string | null;
  /** true = 只看空间根目录（folder_id IS NULL），与 folderId 互斥 */
  rootOnly?: boolean;
  limit?: number;
  offset?: number;
}

/** 文档分页列表：不传 limit 仍返回全量；响应头 X-Total-Count 给出总数。 */
export async function fetchDocumentsPage(
  params: DocumentListParams = {},
): Promise<DocumentPage> {
  const { kbId, space, folderId, rootOnly, limit, offset } = params;
  const { data, headers } = await api.get<DocumentItem[]>('/documents', {
    params: {
      ...(kbId ? { kb_id: kbId } : {}),
      ...(space ? { space } : {}),
      ...(folderId ? { folder_id: folderId } : {}),
      ...(rootOnly ? { root_only: true } : {}),
      ...(limit != null ? { limit } : {}),
      ...(offset != null ? { offset } : {}),
    },
  });
  const raw = headers['x-total-count'];
  const total = raw != null ? Number(raw) : data.length;
  return { items: data, total: Number.isFinite(total) ? total : data.length };
}

/**
 * 上传单个文档（multipart/form-data）。
 *
 * - document 空间：上传后后端异步派发解析任务
 * - structured 空间：只保存文件，不解析、不向量化
 * - folderId：落进当前文件夹；不传 = 空间根目录
 */
export async function uploadDocument(
  file: File,
  kbId: string,
  onProgress?: (percent: number) => void,
  options?: { space?: FileSpace; folderId?: string | null },
): Promise<DocumentItem> {
  const form = new FormData();
  form.append('kb_id', kbId);
  form.append('file', file);
  if (options?.space) form.append('space', options.space);
  if (options?.folderId) form.append('folder_id', options.folderId);
  const { data } = await api.post<DocumentItem>('/documents/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    onUploadProgress: (e) => {
      if (e.total && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
    },
    timeout: 120_000, // 大文件上传放宽超时
  });
  return data;
}

/** 删除文档及其向量数据。 */
export async function deleteDocument(docId: string): Promise<void> {
  await api.delete(`/documents/${docId}`);
}

/**
 * 移动文件到指定文件夹（POST /documents/{id}/move）。
 *
 * 后端只改 folder_id，storage_path 与物理文件不动：不重复上传、不搬文件。
 * folderId 传 null 表示移回该空间的根目录。
 */
export async function moveDocument(
  docId: string,
  folderId: string | null,
): Promise<DocumentItem> {
  const { data } = await api.post<DocumentItem>(`/documents/${docId}/move`, {
    folder_id: folderId,
  });
  return data;
}

/** 重命名文件（PATCH /documents/{id}）：只改展示名，扩展名必须保持不变。 */
export async function renameDocument(docId: string, fileName: string): Promise<DocumentItem> {
  const { data } = await api.patch<DocumentItem>(`/documents/${docId}`, { file_name: fileName });
  return data;
}

/** 手动触发解析/重新解析（失败重试入口，202 Accepted）。 */
export async function reparseDocument(docId: string): Promise<DocumentItem> {
  const { data } = await api.post<DocumentItem>(`/documents/${docId}/parse`);
  return data;
}

/** 重新向量化（success/completed 才允许，409 拒绝未就绪）。 */
export async function reindexDocument(docId: string): Promise<DocumentItem> {
  const { data } = await api.post<DocumentItem>(`/documents/${docId}/reindex`);
  return data;
}

// ─────────────────────────── 文件夹管理 ───────────────────────────
// 每个文件空间一棵独立的文件夹树；文件夹只负责组织文件，不重复保存内容。

/** 该空间全部文件夹（扁平列表 + 直接子级计数），前端按 parent_id 组树。 */
export async function fetchFolders(space: FileSpace): Promise<FileFolderItem[]> {
  const { data } = await api.get<FileFolderItem[]>('/folders', { params: { space } });
  return data;
}

/** 新建文件夹（同空间同层级重名会被后端拒绝）。 */
export async function createFolder(payload: {
  name: string;
  space: FileSpace;
  parent_id?: string | null;
}): Promise<FileFolderItem> {
  const { data } = await api.post<FileFolderItem>('/folders', payload);
  return data;
}

/**
 * 重命名和/或移动整个文件夹（PATCH /folders/{id}）。
 *
 * parent_id 显式传 null = 移到根目录；不传该字段 = 不动父级。
 * 子目录随父级一起移动，后端会拦住成环与目标重名。
 */
export async function updateFolder(
  folderId: string,
  payload: { name?: string; parent_id?: string | null },
): Promise<FileFolderItem> {
  const { data } = await api.patch<FileFolderItem>(`/folders/${folderId}`, payload);
  return data;
}

/** 删除文件夹：后端只允许删除空目录（无子文件夹且无文件），非空返回 400 并说明内容。 */
export async function deleteFolder(folderId: string): Promise<{ id: string; message: string }> {
  const { data } = await api.delete<{ id: string; message: string }>(`/folders/${folderId}`);
  return data;
}

/**
 * 按相对路径逐级确保目录存在（POST /folders/ensure），返回最深一级目录。
 *
 * 上传文件夹批量导入时用它还原原始目录结构：同一批文件共享一次调用结果，
 * 已存在的层级直接复用，不会被重复创建。
 */
export async function ensureFolderPath(
  path: string,
  space: FileSpace,
): Promise<FileFolderItem> {
  const { data } = await api.post<FileFolderItem>('/folders/ensure', { path, space });
  return data;
}

// ─────────────────────────── 结构化数据解析（第三阶段） ───────────────────────────
// 只读取「文件 → 字段识别 → 记录生成」的结果；文件管理仍走 documents / folders。

/** 解析概览：字段清单 + 记录/空值/异常统计（无解析记录时返回 pending 空概览）。 */
export async function fetchStructuredParse(docId: string): Promise<StructuredParseInfo> {
  const { data } = await api.get<StructuredParseInfo>(`/structured/${docId}/parse`);
  return data;
}

/** 记录分页浏览：每次只取一页，后端按文件内生成顺序排序。 */
export async function fetchStructuredRecords(
  docId: string,
  params: { limit?: number; offset?: number; keyword?: string; onlyAnomaly?: boolean } = {},
): Promise<StructuredRecordPage> {
  const { limit = 20, offset = 0, keyword, onlyAnomaly } = params;
  const { data } = await api.get<StructuredRecordPage>(`/structured/${docId}/records`, {
    params: {
      limit,
      offset,
      ...(keyword ? { keyword } : {}),
      ...(onlyAnomaly ? { only_anomaly: true } : {}),
    },
  });
  return data;
}

/** 单条记录详情：原始字段与值 + 来源（文件 / 文件夹 / 行号或 JSON 路径）。 */
export async function fetchStructuredRecord(
  docId: string,
  recordId: string,
): Promise<StructuredRecordDetail> {
  const { data } = await api.get<StructuredRecordDetail>(`/structured/${docId}/records/${recordId}`);
  return data;
}

/** 手动重新解析结构化文件（202 Accepted，返回派发时的当前概览）。 */
export async function reparseStructured(docId: string): Promise<StructuredParseInfo> {
  const { data } = await api.post<StructuredParseInfo>(`/structured/${docId}/reparse`);
  return data;
}

/** 手动重建结构化文件的向量索引（202 Accepted，返回派发时的当前概览）。 */
export async function revectorizeStructured(docId: string): Promise<StructuredParseInfo> {
  const { data } = await api.post<StructuredParseInfo>(`/structured/${docId}/revectorize`);
  return data;
}

// ───────────────── 统一处理概览（第五阶段） ─────────────────
// 只读派生：不触发解析、不产生结果，也不新建任务表。
// 三条链路（文档 / 结构化 / 关联文件组）用同一套状态词汇与阶段结构。

/** 单个文件的处理概览：处理过没有、走到哪步、为什么失败、产生了什么结果、是否重复。 */
export async function fetchDocumentProcessing(docId: string): Promise<DocumentProcessingInfo> {
  const { data } = await api.get<DocumentProcessingInfo>(`/documents/${docId}/processing`);
  return data;
}

// ───────────────── 关联结构化数据（第四阶段） ─────────────────
// 一个文件组 = 一个独立的关联导入任务。文件上传仍走 documents，
// 这里只负责组、关系配置、校验与确认后生成的关联记录。

/** 文件组列表（可按知识库过滤）。 */
export async function fetchStructuredGroups(kbId?: string): Promise<StructuredGroup[]> {
  const { data } = await api.get<StructuredGroup[]>('/structured/groups', {
    params: kbId ? { kb_id: kbId } : {},
  });
  return data;
}

/** 文件组详情：成员结构识别 + 关系配置 + 校验报告。 */
export async function fetchStructuredGroup(groupId: string): Promise<StructuredGroupDetail> {
  const { data } = await api.get<StructuredGroupDetail>(`/structured/groups/${groupId}`);
  return data;
}

/**
 * 文件组处理概览（统一状态与阶段）：比详情轻，供抽屉轮询成员解析进度。
 * 不触发任何解析或生成，因此反复调用无副作用。
 */
export async function fetchGroupProcessing(groupId: string): Promise<GroupProcessingInfo> {
  const { data } = await api.get<GroupProcessingInfo>(`/structured/groups/${groupId}/processing`);
  return data;
}

/** 建组：至少 2 个结构化文件组成一个关联数据文件组（状态 draft，不会自动入库关系）。 */
export async function createStructuredGroup(payload: {
  kb_id: string;
  name: string;
  doc_ids: string[];
}): Promise<StructuredGroup> {
  const { data } = await api.post<StructuredGroup>('/structured/groups', payload);
  return data;
}

export async function renameStructuredGroup(
  groupId: string,
  name: string,
): Promise<StructuredGroup> {
  const { data } = await api.patch<StructuredGroup>(`/structured/groups/${groupId}`, { name });
  return data;
}

export async function deleteStructuredGroup(groupId: string): Promise<{ deleted: boolean }> {
  const { data } = await api.delete<{ deleted: boolean }>(`/structured/groups/${groupId}`);
  return data;
}

/** 向文件组补充成员文件（组构成变化后需重新校验关系）。 */
export async function addStructuredGroupFiles(
  groupId: string,
  docIds: string[],
): Promise<StructuredGroup> {
  const { data } = await api.post<StructuredGroup>(`/structured/groups/${groupId}/files`, {
    doc_ids: docIds,
  });
  return data;
}

/** 给组内文件起角色名（如「学生表」「选课表」），只影响关联文本可读性。 */
export async function setStructuredGroupFileAlias(
  groupId: string,
  docId: string,
  alias: string,
): Promise<StructuredGroup> {
  const { data } = await api.patch<StructuredGroup>(`/structured/groups/${groupId}/files/${docId}`, {
    alias,
  });
  return data;
}

/** 从文件组移出成员（不删除文件本身；引用它的关系一并清理，不留无效关系）。 */
export async function removeStructuredGroupFile(
  groupId: string,
  docId: string,
): Promise<StructuredGroup> {
  const { data } = await api.delete<StructuredGroup>(`/structured/groups/${groupId}/files/${docId}`);
  return data;
}

/** 推测跨文件关系：返回的每条都是 confirmed=false 的候选，不会自动入库。 */
export async function suggestStructuredRelations(groupId: string): Promise<StructuredRelation[]> {
  const { data } = await api.post<StructuredRelation[]>(`/structured/groups/${groupId}/suggest`);
  return data;
}

/** 整份保存关系配置（用户勾选确认的推测结果 或 手动选择的字段）。 */
export async function saveStructuredRelations(
  groupId: string,
  relations: Omit<StructuredRelation, 'id'>[],
): Promise<StructuredGroup> {
  const { data } = await api.put<StructuredGroup>(`/structured/groups/${groupId}/relations`, {
    relations,
  });
  return data;
}

/** 逐条确认/取消确认一条关系（推测的关系只有在这里才能变成已确认）。 */
export async function setStructuredRelationConfirmation(
  groupId: string,
  relationId: string,
  confirmed: boolean,
): Promise<StructuredGroup> {
  const { data } = await api.post<StructuredGroup>(
    `/structured/groups/${groupId}/relations/confirmation`,
    { relation_id: relationId, confirmed },
  );
  return data;
}

/** 正式生成前的校验：只报告问题（主键重复/外键缺失/无法匹配/孤立/类型不一致），不改数据。 */
export async function validateStructuredGroup(groupId: string): Promise<StructuredGroupValidation> {
  const { data } = await api.post<StructuredGroupValidation>(
    `/structured/groups/${groupId}/validate`,
  );
  return data;
}

/** 用户确认后生成正式关联记录；存在未匹配记录时须显式 force。 */
export async function confirmStructuredGroup(
  groupId: string,
  params: { anchorDocId?: string; force?: boolean } = {},
): Promise<{ record_count: number; stats: Record<string, unknown> }> {
  const { data } = await api.post<{ record_count: number; stats: Record<string, unknown> }>(
    `/structured/groups/${groupId}/confirm`,
    {
      anchor_doc_id: params.anchorDocId ?? null,
      force: params.force ?? false,
    },
  );
  return data;
}

/** 手动重建文件组的向量索引（202 Accepted；仅已确认且有正式关联记录的组允许）。 */
export async function revectorizeStructuredGroup(groupId: string): Promise<StructuredGroup> {
  const { data } = await api.post<StructuredGroup>(
    `/structured/groups/${groupId}/revectorize`,
  );
  return data;
}

/** 关联记录分页浏览。 */
export async function fetchStructuredGroupRecords(
  groupId: string,
  params: { limit?: number; offset?: number; keyword?: string; onlyAnomaly?: boolean } = {},
): Promise<StructuredGroupRecordPage> {
  const { limit = 20, offset = 0, keyword, onlyAnomaly } = params;
  const { data } = await api.get<StructuredGroupRecordPage>(`/structured/groups/${groupId}/records`, {
    params: {
      limit,
      offset,
      ...(keyword ? { keyword } : {}),
      ...(onlyAnomaly ? { only_anomaly: true } : {}),
    },
  });
  return data;
}

/** 关联记录详情：参与文件、原始值、行号/JSON 路径、参与关联的字段与关系。 */
export async function fetchStructuredGroupRecord(
  groupId: string,
  recordId: string,
): Promise<StructuredGroupRecordDetail> {
  const { data } = await api.get<StructuredGroupRecordDetail>(
    `/structured/groups/${groupId}/records/${recordId}`,
  );
  return data;
}

// ─────────────────────────── 反馈收集 ───────────────────────────
// PRD §3.6 / §8：用户对助手消息点赞/踩 + 文本纠错意见

export interface FeedbackPayload {
  message_id: string;
  rating: 1 | -1;
  comment?: string;
}

export interface FeedbackResult {
  id: string;
  message_id: string;
  rating: number;
  comment: string;
}

/** 提交反馈（点赞/踩 + 可选纠错意见）；同一消息重复提交会覆盖更新。 */
export async function submitFeedback(
  payload: FeedbackPayload,
): Promise<FeedbackResult> {
  const { data } = await api.post<FeedbackResult>('/feedbacks', payload);
  return data;
}

/** 查询某条消息的已有反馈（无则返回 null）。 */
export async function fetchFeedback(
  messageId: string,
): Promise<FeedbackResult | null> {
  const { data } = await api.get<FeedbackResult | null>(`/feedbacks/${messageId}`);
  return data;
}

// ─────────────────────────── 系统仪表盘 ───────────────────────────
// PRD §5.2：总文档数 / 知识库数 / 累计问答数 / 平均响应延迟（仅 admin）

export interface AdminStats {
  total_docs: number;
  total_kbs: number;
  total_qa: number;
  avg_latency_ms: number;
}

/** 系统仪表盘全局统计（GET /admin/stats，仅 admin 可访问）。 */
export async function fetchAdminStats(): Promise<AdminStats> {
  const { data } = await api.get<AdminStats>('/admin/stats');
  return data;
}

// ─────────────────────────── 模型配置 ───────────────────────────

export interface ModelConfig {
  llm_provider: 'deepseek' | 'openai' | 'qwen' | 'ollama' | 'custom';
  llm_base_url: string;
  llm_model: string;
  llm_api_key: string; // 脱敏值：sk-****xxxx
  /** 后端是否已保存过 API Key（用于前端显示"已配置 / 未配置"）。 */
  llm_api_key_configured?: boolean;
  /** 与 llm_api_key 相同的脱敏展示值（后端新增，避免前端自行解析）。 */
  llm_api_key_masked?: string;
  // Embedding / Rerank / HyDE 均为真实实现（模型随镜像内置），无 mock 选项
  embedding_backend: 'flagembedding';
  embedding_model: string;
  embedding_device: 'cpu' | 'cuda';
  rerank_backend: 'flagreranker';
  rerank_model: string;
  rerank_device: 'cpu' | 'cuda';
  hyde_enabled: boolean;
  hyde_backend: 'openai';
  hyde_model: string;
  hyde_base_url: string;
}

export interface TestConnectionResult {
  ok: boolean;
  message: string;
  latency_ms?: number;
}

/** 获取当前模型配置（API key 已脱敏）。 */
export async function fetchModelConfig(): Promise<ModelConfig> {
  const { data } = await api.get<ModelConfig>('/settings/model');
  return data;
}

export interface PersistenceInfo {
  mode: string;
  host_dir: string;
  uploads_dir: string;
  container_uploads_dir: string;
  services: Record<string, string>;
  note: string;
}

/** 数据持久化状态（管理员）：宿主机数据目录与各服务路径。 */
export async function fetchPersistenceInfo(): Promise<PersistenceInfo> {
  const { data } = await api.get<PersistenceInfo>('/settings/persistence');
  return data;
}

/** 更新模型配置（仅 admin）。API key 传脱敏值则保留原值。 */
export async function updateModelConfig(payload: Partial<ModelConfig>): Promise<ModelConfig> {
  const { data } = await api.put<ModelConfig>('/settings/model', payload);
  return data;
}

/** 测试 LLM 连通性（不保存配置，直接用提交的参数发请求）。 */
export async function testModelConnection(
  payload: Partial<ModelConfig>,
): Promise<TestConnectionResult> {
  const { data } = await api.post<TestConnectionResult>('/settings/model/test', payload);
  return data;
}

// ── Embedding 状态 / 批量重新向量化 ──────────────────────────────────────────

/**
 * Embedding 运行状态：ready（可用）/ revectorizing（正在重建存量向量）。
 * 模型权重与依赖随镜像内置，不存在"待安装 / 初始化"状态。
 */
export type EmbeddingDependencyState = 'ready' | 'revectorizing';

export interface RevectorizeJobStatus {
  job_id: string;
  status: 'pending' | 'running' | 'succeeded' | 'partial' | 'cancelled' | 'failed';
  job_type?: 'document' | 'resource';
  batch_size?: number;
  total_batches?: number;
  current_batch?: number;
  target_model: string;
  previous_model: string | null;
  kb_id: string | null;
  total: number;
  processed: number;
  succeeded: number;
  failed: number;
  failed_doc_ids: string[];
  processing: number;
  percent: number;
  error_message: string;
  created_at: string | null;
  finished_at: string | null;
}

export interface EmbeddingStatus {
  config: {
    backend: string | null;
    model: string | null;
    device: string | null;
    model_key: string;
  };
  vectors: {
    current_model_key: string;
    /** 仍在使用中的文档总数（B-2：未软删除 + 非回收站知识库，仅作状态卡片分母） */
    total_documents: number;
    /** 待重新向量化总数（= stale_with_vectors + unvectorized_documents） */
    stale_documents: number;
    /** 真正的旧模型向量（Milvus 中仍存在，检索时被自动排除） */
    stale_with_vectors: number;
    /** 从未真正向量化（Milvus 中无向量，如历史导入占位标记） */
    unvectorized_documents: number;
    stale_models: string[];
  };
  job: RevectorizeJobStatus | null;
  overall_status: EmbeddingDependencyState;
}

/** Embedding 运行状态总览（依赖 / 存量向量 / 待重建数量 / 任务进度）。 */
export async function fetchEmbeddingStatus(): Promise<EmbeddingStatus> {
  const { data } = await api.get<EmbeddingStatus>('/settings/model/embedding/status');
  return data;
}

/** 发起批量重新向量化（仅 admin）；kb_id 为空表示全库。 */
export async function startRevectorize(kbId?: string): Promise<RevectorizeJobStatus> {
  const { data } = await api.post<RevectorizeJobStatus>('/settings/model/revectorize', {
    kb_id: kbId ?? null,
  });
  return data;
}

/** 查询最近一次批量重新向量化任务进度。 */
export async function fetchRevectorizeStatus(): Promise<RevectorizeJobStatus | null> {
  const { data } = await api.get<RevectorizeJobStatus | null>('/settings/model/revectorize/status');
  return data;
}

/** 重试指定任务的失败项（仅 admin）。 */
export async function retryRevectorize(jobId: string): Promise<RevectorizeJobStatus> {
  const { data } = await api.post<RevectorizeJobStatus>('/settings/model/revectorize/retry', {
    job_id: jobId,
  });
  return data;
}

/**
 * 取消进行中的批量重新向量化（仅 admin）。
 * 协作式：已完成的文档保留新向量，剩余文档保持旧向量，不回滚已完成部分。
 */
export async function cancelRevectorize(jobId: string): Promise<RevectorizeJobStatus> {
  const { data } = await api.post<RevectorizeJobStatus>('/settings/model/revectorize/cancel', {
    job_id: jobId,
  });
  return data;
}


// ─────────────────────────── 用户管理 ───────────────────────────

/** 创建用户请求体。 */
export interface UserCreatePayload {
  username: string;
  email: string;
  name?: string;
  department?: string;
  role?: 'admin' | 'member' | 'viewer';
}

/** 创建用户响应（含初始密码）。 */
export interface UserCreateResponse {
  user: UserOut;
  initial_password: string;
}

/** 更新用户请求体。 */
export interface UserUpdatePayload {
  name?: string;
  department?: string;
  role?: 'admin' | 'member' | 'viewer';
}

/** 批量导入结果。 */
export interface BatchImportResult {
  total: number;
  success: number;
  failed: number;
  errors: string[];
}

/** 重置密码响应。 */
export interface ResetPasswordResponse {
  user_id: string;
  username: string;
  new_password: string;
}

/** 批量删除结果。 */
export interface BatchDeleteResult {
  total: number;
  success: number;
  message: string;
}

/** 批量删除预检失败返回。 */
export interface BatchDeleteError {
  message: string;
  errors: string[];
}

/** 用户列表（全量，兼容既有调用方）。 */
export async function fetchUsers(): Promise<UserOut[]> {
  const { data } = await api.get<UserOut[]>('/users');
  return data;
}

/**
 * 服务端分页的用户列表（卡顿修复）。
 *
 * 后端 `/users` 支持可选 `limit`/`offset`，并在响应头 `X-Total-Count` 返回活跃用户总数：
 * 用户管理页改用它后，一次只拉当前页（默认 20 条），不再拉回全部 846 条后由
 * AntD 做客户端分页。
 *
 * `total` 为 null 表示响应头缺失（例如被代理剥离），调用方应回退为客户端分页。
 */
export interface UserPage {
  users: UserOut[];
  total: number | null;
}

export async function fetchUsersPage(params: {
  limit: number;
  offset: number;
}): Promise<UserPage> {
  const res = await api.get<UserOut[]>('/users', { params });
  const raw = res.headers?.['x-total-count'];
  const parsed = raw === undefined || raw === null ? Number.NaN : Number(raw);
  return { users: res.data, total: Number.isFinite(parsed) ? parsed : null };
}

/** 创建用户（系统自动生成初始密码）。 */
export async function createUser(payload: UserCreatePayload): Promise<UserCreateResponse> {
  const { data } = await api.post<UserCreateResponse>('/users', payload);
  return data;
}

/** 更新用户信息。 */
export async function updateUser(
  userId: string,
  payload: UserUpdatePayload,
): Promise<UserOut> {
  const { data } = await api.put<UserOut>(`/users/${userId}`, payload);
  return data;
}

/** 删除用户。 */
export async function deleteUser(userId: string): Promise<void> {
  await api.delete(`/users/${userId}`);
}

/** 管理员重置用户密码（返回新随机密码）。 */
export async function resetUserPassword(userId: string): Promise<ResetPasswordResponse> {
  const { data } = await api.post<ResetPasswordResponse>(`/users/${userId}/reset-password`);
  return data;
}

/** 批量删除用户（全量预检，有错误则抛 400 + 错误详情）。 */
export async function batchDeleteUsers(userIds: string[]): Promise<BatchDeleteResult> {
  const { data } = await api.post<BatchDeleteResult>('/users/batch-delete', { user_ids: userIds });
  return data;
}

/** 回收站用户列表。 */
export async function fetchTrashUsers(): Promise<UserOut[]> {
  const { data } = await api.get<UserOut[]>('/users/trash');
  return data;
}

/** 从回收站恢复用户。 */
export async function restoreUser(userId: string): Promise<UserOut> {
  const { data } = await api.post<UserOut>(`/users/${userId}/restore`);
  return data;
}

/** 批量恢复回收站用户（全量预检，有错误则抛 400 + 错误详情）。 */
export async function batchRestoreUsers(userIds: string[]): Promise<{ total: number; success: number; message: string }> {
  const { data } = await api.post('/users/batch-restore', { user_ids: userIds });
  return data;
}

/** 彻底删除用户（不可恢复）。 */
export async function purgeUser(userId: string): Promise<void> {
  await api.delete(`/users/${userId}/purge`);
}

/** 批量导入用户（CSV 文件）。 */
export async function batchImportUsers(file: File): Promise<BatchImportResult> {
  const form = new FormData();
  form.append('file', file);
  const { data } = await api.post<BatchImportResult>('/users/batch', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

/** 下载批量导入 CSV 模板。 */
export async function downloadUserTemplate(): Promise<void> {
  const resp = await api.get('/users/template', { responseType: 'blob' });
  const blob = new Blob([resp.data], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'user_import_template.csv';
  a.click();
  URL.revokeObjectURL(url);
}

/** 禁用用户账号（BUSINESS_RULES §2 离职处理=禁用）。 */
export async function disableUser(userId: string): Promise<void> {
  await api.post(`/users/${userId}/disable`);
}

/** 启用用户账号（恢复已禁用的用户）。 */
export async function enableUser(userId: string): Promise<void> {
  await api.post(`/users/${userId}/enable`);
}

// ─────────────────────────── 知识库成员管理 ───────────────────────────

/** 知识库成员列表。 */
export async function fetchKbMembers(kbId: string): Promise<KBMember[]> {
  const { data } = await api.get<KBMember[]>(`/kb/${kbId}/members`);
  return data;
}

/** 添加知识库成员。 */
export async function addKbMember(
  kbId: string,
  userId: string,
  role: KBMemberRole,
): Promise<KBMember> {
  const { data } = await api.post<KBMember>(`/kb/${kbId}/members`, {
    user_id: userId,
    role,
  });
  return data;
}

/** 批量添加知识库成员。 */
export async function batchAddKbMembers(
  kbId: string,
  userIds: string[],
  role: KBMemberRole,
): Promise<{ added: number; skipped: number }> {
  const { data } = await api.post(`/kb/${kbId}/members/batch`, {
    user_ids: userIds,
    role,
  });
  return data;
}

/** 移除知识库成员。 */
export async function removeKbMember(
  kbId: string,
  userId: string,
): Promise<void> {
  await api.delete(`/kb/${kbId}/members/${userId}`);
}

/** 修改成员角色。 */
export async function updateKbMemberRole(
  kbId: string,
  userId: string,
  role: KBMemberRole,
): Promise<KBMember> {
  const { data } = await api.put<KBMember>(`/kb/${kbId}/members/${userId}`, {
    role,
  });
  return data;
}

/** 转移知识库所有权。 */
export async function transferKbOwnership(
  kbId: string,
  newOwnerId: string,
): Promise<void> {
  await api.post(`/kb/${kbId}/transfer-ownership`, {
    new_owner_user_id: newOwnerId,
  });
}

// ─────────────────────────── 知识库回收站 ───────────────────────────

/** 回收站知识库列表。 */
export async function fetchTrashKbs(): Promise<KnowledgeBase[]> {
  const { data } = await api.get<KnowledgeBase[]>('/kb/trash');
  return data;
}

/** 恢回收站知识库。 */
export async function restoreKb(kbId: string): Promise<KnowledgeBase> {
  const { data } = await api.post<KnowledgeBase>(`/kb/${kbId}/restore`);
  return data;
}

/** 彻底删除知识库（不可恢复）。 */
export async function purgeKb(kbId: string): Promise<void> {
  await api.delete(`/kb/${kbId}/purge`);
}


// ─────────────────────────── 文档回收站 ───────────────────────────

/** 回收站文档列表。 */
export async function fetchTrashDocuments(): Promise<DocumentItem[]> {
  const { data } = await api.get<DocumentItem[]>('/documents/trash/list');
  return data;
}

/** 恢回收站文档。 */
export async function restoreDocument(docId: string): Promise<DocumentItem> {
  const { data } = await api.post<DocumentItem>(`/documents/${docId}/restore`);
  return data;
}

/** 彻底删除文档（不可恢复）。 */
export async function purgeDocument(docId: string): Promise<void> {
  await api.delete(`/documents/${docId}/purge`);
}

// ─────────────────────────── 审计日志 ───────────────────────────

/** 查询审计日志（仅 admin，支持筛选）。 */
export async function fetchAuditLogs(params?: {
  operation?: string;
  target_type?: string;
  start_time?: string;
  end_time?: string;
  limit?: number;
  offset?: number;
}): Promise<AuditLog[]> {
  const { data } = await api.get<AuditLog[]>('/audit', { params });
  return data;
}

// ─────────────────────────── 系统配置 ───────────────────────────

/** 获取系统级配置。 */
export async function fetchSystemConfig(): Promise<SystemConfig> {
  const { data } = await api.get<SystemConfig>('/settings/system');
  return data;
}

/** 更新系统级配置。 */
export async function updateSystemConfig(
  payload: Partial<SystemConfigValues>,
): Promise<SystemConfig> {
  const { data } = await api.put<SystemConfig>('/settings/system', payload);
  return data;
}

/** 恢复系统级配置为后端默认值。 */
export async function resetSystemConfig(): Promise<SystemConfig> {
  const { data } = await api.post<SystemConfig>('/settings/system/reset');
  return data;
}


// ─────────────────────────── 批量操作结果 ───────────────────────────

/** 批量操作返回结构（成功数 + 失败明细）。 */
export interface BatchOpResult {
  total?: number;
  success?: number;
  purged?: number;
  message?: string;
  failed?: { id: string; reason: string }[];
}

/** 用户回收站一键清空（仅 admin）。 */
export async function purgeAllUserTrash(): Promise<BatchOpResult> {
  const { data } = await api.delete<BatchOpResult>('/users/trash/purge-all');
  return data;
}

// ─────────────────────────── 知识库批量操作 ───────────────────────────

/** 知识库批量删除 → 回收站。 */
export async function batchDeleteKbs(kbIds: string[]): Promise<BatchOpResult> {
  const { data } = await api.post<BatchOpResult>('/kb/batch-delete', { ids: kbIds });
  return data;
}

/** 知识库批量恢复（仅 admin）。 */
export async function batchRestoreKbs(kbIds: string[]): Promise<BatchOpResult> {
  const { data } = await api.post<BatchOpResult>('/kb/batch-restore', { ids: kbIds });
  return data;
}

/** 一键清空知识库回收站（仅 admin）。 */
export async function purgeAllKbTrash(): Promise<BatchOpResult> {
  const { data } = await api.delete<BatchOpResult>('/kb/trash/purge-all');
  return data;
}


