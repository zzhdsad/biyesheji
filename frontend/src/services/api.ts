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

/** 统一 API 客户端：开发环境经 Next.js rewrites 代理到 FastAPI。 */
export const api = axios.create({
  baseURL: '/api/v1',
  timeout: 60_000, // RAG 检索+生成可能较慢，放宽超时
});

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
