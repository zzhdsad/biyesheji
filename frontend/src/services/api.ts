import axios from 'axios';
import type { Citation } from '@/types';

/** 统一 API 客户端：开发环境经 Next.js rewrites 代理到 FastAPI。 */
export const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30_000,
});

export interface ChatAnswer {
  conversation_id: string;
  answer: string;
  citations: Citation[];
}

export async function askQuestion(payload: {
  question: string;
  kb_ids?: string[];
  conversation_id?: string | null;
}): Promise<ChatAnswer> {
  const { data } = await api.post<ChatAnswer>('/chat/ask', payload);
  return data;
}

export async function fetchConversations(): Promise<unknown[]> {
  const { data } = await api.get('/chat/conversations');
  return data;
}
