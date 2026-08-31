/** 共享类型定义（与后端 Pydantic Schema 对应）。 */

export type Role = 'user' | 'assistant';

export interface Citation {
  chunk_id: string;
  source_index: number; // 来源编号（对应答案内联 [citation: 编号, 页码]）
  doc_id: string;
  doc_name: string;
  page_num?: number | null;
  title_path?: string | null;
  content: string;
  score?: number;
}

export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  citations?: Citation[];
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  visibility: 'public' | 'private';
  document_count?: number;
}

export type ParseStatus = 'pending' | 'parsing' | 'success' | 'failed';

export interface DocumentItem {
  id: string;
  kb_id: string;
  file_name: string;
  file_type: string;
  file_size: number;
  parse_status: ParseStatus;
  chunk_count: number;
  created_at: string;
}

/** 后端会话记录（GET /chat/conversations）。 */
export interface Conversation {
  id: string;
  title: string;
  kb_ids: string[];
  created_at: string;
}

/** 后端消息记录（GET /chat/conversations/:id/messages，citations 为 JSONB）。 */
export interface MessageOut {
  id: string;
  role: string;
  content: string;
  citations: { sources?: Citation[] } | null;
  created_at: string;
}

/** 系统健康状态（GET /health）。 */
export interface HealthResponse {
  status: string;
  app?: string;
  version?: string;
  env?: string;
  components: Record<string, string>;
}
