import axios from 'axios';
import type {
  ChatMessage,
  Citation,
  Conversation,
  DocumentItem,
  EvaluationHistoryItem,
  EvaluationReport,
  EvalTestCaseItem,
  HealthResponse,
  KnowledgeBase,
  MessageOut,
  Role,
} from '@/types';
import { clearToken, getToken } from './token';

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
// 用 window.location 硬跳，避免 SPA 路由守卫与拦截器互相触发造成循环
api.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err?.response?.status;
    if (status === 401 && typeof window !== 'undefined') {
      // /login、/register 自身的 401 不跳转（避免登录页请求失败时反复跳转）
      const path = window.location.pathname;
      if (!path.startsWith('/login') && !path.startsWith('/register')) {
        clearToken();
        const redirect = encodeURIComponent(path + window.location.search);
        window.location.href = `/login?redirect=${redirect}`;
      }
    }
    return Promise.reject(err);
  },
);

export interface ChatAnswer {
  conversation_id: string;
  message_id: string;
  answer: string;
  citations: Citation[];
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

/** 上传单个文档（multipart/form-data）。上传后后端异步派发解析任务。 */
export async function uploadDocument(
  file: File,
  kbId: string,
  onProgress?: (percent: number) => void,
): Promise<DocumentItem> {
  const form = new FormData();
  form.append('kb_id', kbId);
  form.append('file', file);
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

// ─────────────────────────── 评估 ───────────────────────────

/** 上传测试集（写入 test_cases，绑定知识库）。 */
export async function uploadTestSet(
  kbId: string,
  cases: EvalTestCaseItem[],
): Promise<{ kb_id: string; uploaded: number; case_ids: string[] }> {
  const { data } = await api.post('/evaluation/upload', { kb_id: kbId, cases });
  return data;
}

/** 运行 RAGAS 评估（批量 RAG + 指标，返回富报告）。评估较慢，放宽超时。 */
export async function runEvaluation(
  kbId: string,
  caseIds?: string[],
): Promise<EvaluationReport> {
  const { data } = await api.post<EvaluationReport>(
    '/evaluation/run',
    { kb_id: kbId, case_ids: caseIds },
    { timeout: 300_000 },
  );
  return data;
}

/** 历史评估结果（关联测试用例，按时间倒序）。 */
export async function fetchEvalHistory(kbId?: string): Promise<EvaluationHistoryItem[]> {
  const { data } = await api.get<EvaluationHistoryItem[]>('/evaluation/results', {
    params: kbId ? { kb_id: kbId } : {},
  });
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
  llm_provider: 'mock' | 'deepseek' | 'openai' | 'qwen' | 'ollama' | 'custom';
  llm_base_url: string;
  llm_model: string;
  llm_api_key: string; // 脱敏值：sk-****xxxx
  embedding_backend: 'mock' | 'flagembedding';
  embedding_model: string;
  embedding_device: 'cpu' | 'cuda';
  rerank_backend: 'mock' | 'flagreranker';
  rerank_model: string;
  rerank_device: 'cpu' | 'cuda';
  hyde_enabled: boolean;
  hyde_backend: 'mock' | 'openai';
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
