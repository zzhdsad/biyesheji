/** 共享类型定义（与后端 Pydantic Schema 对应）。 */

export type Role = 'user' | 'assistant';

export interface Citation {
  doc_id: string;
  doc_name: string;
  page_num?: number | null;
  title_path?: string | null;
  content: string;
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
