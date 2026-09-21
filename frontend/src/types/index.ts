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
  is_active?: boolean;
  created_at?: string | null;
  deleted_at?: string | null;
}

/** 登录响应（POST /auth/login）。 */
export interface TokenResponse {
  access_token: string;
  token_type: 'bearer';
  expires_in: number; // 秒
  user: UserOut;
}

export type Role = 'user' | 'assistant';

/** 中医文献来源类型（与后端 src.core.source_meta 受控枚举一致）。 */
export type SourceType =
  | '国家标准'
  | '规划教材'
  | '经典古籍'
  | '后世医家'
  | '民间偏方';

/** 成书/出版年代（受控枚举）。 */
export type SourceEra = '先秦' | '汉' | '唐' | '宋' | '明' | '清' | '现代';

export interface Citation {
  chunk_id: string;
  source_index: number; // 来源编号（对应答案内联 [citation: 编号, 页码]）
  doc_id: string;
  doc_name: string;
  page_num?: number | null;
  title_path?: string | null;
  content: string;
  score?: number;
  source_type?: SourceType | null;
  era?: SourceEra | null;
  credibility_level?: number | null;
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
  owner_id?: string;
  deleted_at?: string | null;
  document_count?: number;
}

/** 知识库成员角色（BUSINESS_RULES §3）。 */
export type KBMemberRole = 'owner' | 'admin' | 'editor' | 'viewer';

/** 知识库成员。 */
export interface KBMember {
  kb_id: string;
  user_id: string;
  role: KBMemberRole;
}

/** 审计日志（BUSINESS_RULES §7）。 */
export interface AuditLog {
  id: string;
  operator_id?: string | null;
  operator_name: string;
  operation: string;
  target_type: string;
  target_id: string;
  detail: Record<string, unknown>;
  ip: string;
  created_at: string;
}

/** 系统级配置（BUSINESS_RULES §8）。 */
export interface SystemConfig {
  trash_retention_days: number;
  max_file_size_mb: number;
  recall_top_k: number;
  rerank_top_n: number;
  relevance_threshold: number;
  history_window: number;
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
  source_type?: SourceType | null;
  era?: SourceEra | null;
  credibility_level?: number | null;
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

// ─────────────────────────── 分类与标签（TASK-002）───────────────────────────

/** 分类适用的资源域（与后端 taxonomy RESOURCE_TYPES 一致，区别于中文 SourceType）。 */
export type TaxonomyResourceType = 'herb' | 'prescription' | 'theory' | 'literature';

/** 分类（tree=true 时 children 有值）。 */
export interface Category {
  id: string;
  resource_type: TaxonomyResourceType;
  name: string;
  parent_id: string | null;
  sort_order: number;
  description: string;
  created_at: string;
  updated_at: string;
  children?: Category[];
}

/** 标签（扁平、跨资源域）。 */
export interface Tag {
  id: string;
  name: string;
  color: string;
  description: string;
  created_at: string;
  updated_at: string;
}

// ─────────────────────────── 中药（TASK-003）───────────────────────────

/** 中药资源（与后端 HerbOut 对应）。 */
export interface Herb {
  id: string;
  name: string;
  aliases: string[];
  category_id: string | null;
  category: Pick<Category, 'id' | 'name'> | null;
  properties: string;
  channels: string[];
  effects: string;
  source: string;
  description: string;
  tags: Pick<Tag, 'id' | 'name' | 'color'>[];
  created_at: string;
  updated_at: string;
}

/** 中药分页响应（GET /herbs）。 */
export interface HerbListResponse {
  items: Herb[];
  total: number;
  limit: number;
  offset: number;
}

// ─────────────────────────── 方剂（TASK-004）───────────────────────────

/** 方剂组成中的一味药材（与后端 PrescriptionIngredientOut 对应）。 */
export interface PrescriptionIngredient {
  id: string;
  herb_id: string;
  herb_name: string;
  amount: number | null;
  unit: string;
  processing: string;
  role: string;
  sort_order: number;
}

/** 方剂资源（与后端 PrescriptionOut 对应）。 */
export interface Prescription {
  id: string;
  name: string;
  aliases: string[];
  category_id: string | null;
  category: Pick<Category, 'id' | 'name'> | null;
  efficacy: string;
  indications: string;
  usage_method: string;
  source: string;
  description: string;
  ingredients: PrescriptionIngredient[];
  tags: Pick<Tag, 'id' | 'name' | 'color'>[];
  created_at: string;
  updated_at: string;
}

/** 方剂分页响应（GET /prescriptions）。 */
export interface PrescriptionListResponse {
  items: Prescription[];
  total: number;
  limit: number;
  offset: number;
}

// ─────────────────────────── 中医理论（TASK-005）───────────────────────────

/** 中医理论资源（与后端 TheoryOut 对应）。 */
export interface Theory {
  id: string;
  name: string;
  aliases: string[];
  category_id: string | null;
  category: Pick<Category, 'id' | 'name'> | null;
  content: string;
  source: string;
  tags: Pick<Tag, 'id' | 'name' | 'color'>[];
  created_at: string;
  updated_at: string;
}

/** 理论分页响应（GET /theories）。 */
export interface TheoryListResponse {
  items: Theory[];
  total: number;
  limit: number;
  offset: number;
}

// ─────────────────────────── 中医文献（TASK-006）───────────────────────────

/** 中医文献资源（与后端 LiteratureOut 对应）。 */
export interface Literature {
  id: string;
  name: string;
  aliases: string[];
  category_id: string | null;
  category: Pick<Category, 'id' | 'name'> | null;
  author: string;
  dynasty: string;
  summary: string;
  content: string;
  source: string;
  tags: Pick<Tag, 'id' | 'name' | 'color'>[];
  created_at: string;
  updated_at: string;
}

/** 文献分页响应（GET /literatures）。 */
export interface LiteratureListResponse {
  items: Literature[];
  total: number;
  limit: number;
  offset: number;
}
