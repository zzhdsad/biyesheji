import axios from 'axios';
import type { ChatMessage, Citation, Conversation, HealthResponse, KnowledgeBase, MessageOut, Role } from '@/types';

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
