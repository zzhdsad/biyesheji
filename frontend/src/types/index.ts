/** 共享类型定义（与后端 Pydantic Schema 对应）。 */

// ─────────────────────────── 鉴权 ───────────────────────────

/** 用户角色（与后端 User.role 对应）。 */
export type UserRole = 'admin' | 'member' | 'viewer';

/** 对外暴露的用户信息（GET /auth/me、login.user，不含密码）。 */
export interface UserOut {
  id: string;
  email: string;
  username: string;
  role: UserRole;
  name?: string;
  department?: string;
  must_change_password?: boolean;
  created_at?: string | null;
}

/** 登录响应（POST /auth/login）。 */
export interface TokenResponse {
  access_token: string;
  token_type: 'bearer';
  expires_in: number; // 秒
  user: UserOut;
}

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

/** pending=待解析 / parsing=解析中 / success=切片就绪 / completed=已向量化 / failed=失败。 */
export type ParseStatus = 'pending' | 'parsing' | 'success' | 'completed' | 'failed';

export interface DocumentItem {
  id: string;
  kb_id: string;
  file_name: string;
  file_type: string;
  file_size: number;
  parse_status: ParseStatus;
  chunk_count: number;
  error_message?: string;
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

// ─────────────────────────── 评估 ───────────────────────────

/** 测试集条目（上传/评估输入）。 */
export interface EvalTestCaseItem {
  question: string;
  golden_answer: string;
  golden_contexts?: string[];
}

/** 单条用例评估结果（run 报告明细）。 */
export interface EvalCaseResult {
  test_case_id: string;
  question: string;
  golden_answer: string;
  golden_contexts: string[];
  answer: string;
  retrieved_contexts: string[];
  context_relevancy: number;
  answer_correctness: number;
  error?: string | null;
}

/** 评估报告（POST /evaluation/run）。 */
export interface EvaluationReport {
  run_id: string;
  case_count: number;
  context_relevancy: number;
  answer_correctness: number;
  passed: boolean;
  threshold: number;
  results: EvalCaseResult[];
}

/** 历史评估结果条目（GET /evaluation/results）。 */
export interface EvaluationHistoryItem {
  id: string;
  question: string;
  golden_answer: string;
  answer_correctness: number;
  context_relevancy: number;
  created_at: string | null;
}
