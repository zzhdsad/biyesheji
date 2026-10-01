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

/**
 * 引用来源（同时是统一 Evidence 的载体）。
 *
 * 阶段十：Document 命中与 Resource（herb/prescription/theory/literature）命中
 * 统一到同一结构，旧字段保持不变（历史消息与旧解析逻辑完全兼容），
 * 新增字段均为可选，缺省时由前端 utils/evidence 归一化补齐。
 */
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
  /**
   * 来源类别：document（上传文献） / resource（中药·方剂·理论·文献条目） /
   * kg（阶段十三知识图谱关系证据，后端 application/evidence.py 三值之一）。
   */
  source_kind?: 'document' | 'resource' | 'kg';
  /** 证据等级：high / medium / insufficient */
  evidence_level?: EvidenceLevel;
  /** Resource 专属：资源细分类型 */
  resource_type?: ResourceType | null;
  resource_id?: string | null;
  resource_name?: string | null;
  // ── 阶段十统一 Evidence 访问入口 ──
  evidence_id?: string;
  source_id?: string | null;
  source_name?: string;
  /** 来源分类展示标签：文档 / 中药 / 方剂 / 理论 / 文献 */
  source_label?: string;
  evidence_text?: string;
  // ── 阶段十四：KG 关系属性（BUG-065：非流式路径此前丢失，现后端统一声明）──
  kg_relation?: string | null;
  kg_hop?: number | null;
  kg_provenance?: string | null;
  // ── 相关度展示（BUG-（相关度 3%））：BGE-M3 dense cosine，由后端
  //    application/evidence.py:hit_to_evidence 写入 evidence dict，
  //    /chat/ask-stream 流式 dict 直传已含此字段；/chat/ask 同步路径此前
  //    因 Citation 模型未声明而被 Pydantic 静默丢弃。EvidencePanel 优先
  //    使用此字段展示「相关度」百分比；缺失时 fallback 到 ev.score。
  relevance_score?: number | null;
}

/** 证据等级（沿用后端既有分级规则）。 */
export type EvidenceLevel = 'high' | 'medium' | 'insufficient';

/** Resource 细分类型（与后端 resource_type 一致）。 */
export type ResourceType = 'herb' | 'prescription' | 'theory' | 'literature';

/** 统一 Evidence：字段与 Citation 完全一致，语义上作为证据条目。 */
export type Evidence = Citation;

/** 同一来源（某味中药 / 某篇文献 / 某个文档）下的证据聚合。 */
export interface EvidenceSource {
  source_id?: string | null;
  source_name: string;
  source_kind: string;
  source_type?: string | null;
  source_label?: string;
  evidence_count: number;
  max_score: number;
  evidence_level: EvidenceLevel;
  evidences: Evidence[];
}

/** 多来源证据分组：document / resource:{herb,prescription,theory,literature}。 */
export interface EvidenceGroup {
  group_key: string;
  source_kind: string;
  source_type?: string | null;
  source_label?: string;
  source_count: number;
  evidence_count: number;
  max_score: number;
  evidence_level: EvidenceLevel;
  sources: EvidenceSource[];
}

export interface EvidenceSummary {
  evidence_count: number;
  source_count: number;
  group_count: number;
  max_score: number;
  by_level: Record<EvidenceLevel, number>;
}

/**
 * 阶段十一：Query 分析结果（结构化，与后端 QueryAnalysisOut 对齐）。
 *
 * 仅用于展示/调试；前端不得依据该结果改变请求行为（检索策略由后端决定）。
 * is_unanswerable_candidate 只是"可能无法可靠回答"的候选标记，不代表系统拒答。
 */
export interface QueryAnalysis {
  query: string;
  question_type: string;
  question_type_label: string;
  resource_types?: string[];
  is_multi_source?: boolean;
  is_unanswerable_candidate?: boolean;
  keywords?: string[];
  entities?: { text: string; type: string }[];
  features?: Record<string, unknown>;
  analyzer_version: string;
  is_valid?: boolean;
  fallback_reason?: string | null;
}

/**
 * 阶段十二：Dynamic Router 决策（结构化，与后端 RouterDecisionOut 对齐）。
 *
 * 仅用于解释/调试（"为什么用这个检索策略"）；前端不得据其改变请求行为。
 * fallback 时 strategy_name 仍为 baseline_hybrid，is_valid 为 false。
 */
export interface RouterDecision {
  strategy_name: string;
  reason: string;
  question_type: string;
  resource_types?: string[];
  router_version: string;
  strategy_description?: string;
  resource_filter?: Record<string, unknown>;
  retrieval_config?: Record<string, unknown>;
  is_valid?: boolean;
  fallback_reason?: string | null;
}

/**
 * 阶段十四：Evidence Gate 决策（结构化，与后端 GateDecisionOut 对齐）。
 *
 * decision ∈ {accept, insufficient, retry}：
 * - accept：证据足够，正常生成
 * - insufficient：证据不足，已拒答（证据/引用仍保留）
 * - retry：证据不足但已换策略重试一次（响应中为重试后的最终判定）
 *
 * 仅用于解释/调试；前端不得据其改变请求行为。Gate 关闭时该字段为 null。
 */
export interface GateDecision {
  decision: 'accept' | 'insufficient' | 'retry' | string;
  reason?: string;
  gate_version?: string;
  evidence_count?: number;
  accepted_count?: number;
  high_count?: number;
  medium_count?: number;
  weak_count?: number;
  source_kind_counts?: Record<string, number>;
  best_score?: number;
  is_valid?: boolean;
  fallback_reason?: string | null;
  retry_reason?: string | null;
  original_strategy?: string | null;
  retry_strategy?: string | null;
  retried?: boolean;
  details?: Record<string, unknown>;
}

/**
 * 阶段十五：Self Reflection 决策（结构化，与后端 ReflectionDecisionOut 对齐）。
 *
 * decision ∈ {accept, revise, retry}：
 * - accept：答案与证据一致
 * - revise：答案表达超出证据，已基于同一份证据重写（最多一次，revised=true）
 * - retry：证据不足以支撑答案，已换策略重检索一次（最多一次，retried=true）
 *
 * gate_retry_count / reflection_retry_count / total_retry_count 分别记录两类
 * retry 次数，保证总次数有界（均 ≤ 1 次 Combination）。
 *
 * 仅用于解释/调试；前端不得据其改变请求行为。Reflection 关闭时该字段为 null。
 */
export interface ReflectionDecision {
  decision: 'accept' | 'revise' | 'retry' | string;
  reason?: string;
  reflection_version?: string;
  confidence?: number;
  issues?: string[];
  retry_strategy?: string | null;
  retried?: boolean;
  is_valid?: boolean;
  fallback_reason?: string | null;
  gate_retry_count?: number;
  reflection_retry_count?: number;
  total_retry_count?: number;
  revised?: boolean;
  gate_decision?: string | null;
  gate_version?: string | null;
  original_strategy?: string | null;
  retry_reason?: string | null;
  llm_used?: boolean;
  details?: Record<string, unknown>;
}

export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  citations?: Citation[];
  /** 阶段十：多来源证据（流式由 citations 事件下发；历史消息由 citations 推导） */
  evidence?: Evidence[];
  evidenceGroups?: EvidenceGroup[];
  evidenceSummary?: EvidenceSummary;
  /** 阶段十一：Query 分析（流式由 start 事件下发） */
  queryAnalysis?: QueryAnalysis;
  /** 阶段十二：检索策略路由决策（流式由 start 事件下发） */
  routerDecision?: RouterDecision;
  /** 阶段十三：KG 关系证据切片（流式由 citations 事件下发；citations 已包含，此处为同一份数据的视图） */
  kgEvidence?: Citation[];
  /** 阶段十四：证据门控决策（流式由 citations 事件下发） */
  evidenceGate?: GateDecision;
  /** 阶段十五：自反思决策（流式由 done 事件下发） */
  reflection?: ReflectionDecision;
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

// ─────────────────────────── 知识库资源挂载（TASK-008 Stage 4-7）───────────────────────────

/** 知识库挂载的结构化资源类型（与后端 RESOURCE_TYPES 一致）。 */
export type KBResourceType = 'herb' | 'prescription' | 'theory' | 'literature';

/** 已挂载资源记录（与后端 ResourceMountedOut 对应）。 */
export interface KBResource {
  id: string;
  knowledge_base_id: string;
  resource_type: KBResourceType;
  resource_id: string;
  resource_name: string;
  created_at: string;
}

/** 已挂载资源分页响应（GET /kb/{id}/resources）。 */
export interface KBResourceListResponse {
  items: KBResource[];
  total: number;
  limit: number;
  offset: number;
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

/** 系统级配置取值（BUSINESS_RULES §8）。 */
export interface SystemConfigValues {
  trash_retention_days: number;
  max_file_size_mb: number;
  recall_top_k: number;
  rerank_top_n: number;
  relevance_threshold: number;
  history_window: number;
}

/**
 * 系统级配置响应：在取值之外附带 `defaults`（后端 Settings 默认值），
 * 供界面展示默认值提示与"恢复默认值"。
 */
export interface SystemConfig extends SystemConfigValues {
  defaults?: SystemConfigValues;
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

/** 问题类型受控词表（TASK-009：中医知识问题分类）。 */
export type QuestionType =
  | 'herb'
  | 'prescription'
  | 'theory'
  | 'literature'
  | 'multi_source'
  | 'unanswerable'
  | 'general';

/** 问题类型选项（GET /evaluation/question-types）。 */
export interface QuestionTypeOption {
  value: string;
  label: string;
}

/** 测试集条目（上传/评估输入）。 */
export interface EvalTestCaseItem {
  question: string;
  golden_answer: string;
  golden_contexts?: string[];
  /** 问题分类，默认 general；TASK-011 按类型分析检索策略效果。 */
  question_type?: QuestionType | string;
  /** 数据集版本，不传则使用上传请求级 dataset_version。 */
  dataset_version?: string;
  /** 标准答案是否仍需人工确认；未评估的用例不计入答案正确度。 */
  needs_review?: boolean;
  /** 人工标注依据提示。 */
  source_reference?: string;
}

/** 测试集条目（GET /evaluation/test-cases，含服务端状态）。 */
export interface EvalTestCaseOut extends EvalTestCaseItem {
  id: string;
  kb_id: string;
  golden_answer: string;
  golden_contexts: string[];
  question_type: string;
  question_type_label: string;
  dataset_version: string;
  needs_review: boolean;
  source_reference: string;
  created_at: string | null;
}

/** 按问题类型分组的指标。 */
export interface QuestionTypeMetric {
  question_type: string;
  question_type_label: string;
  case_count: number;
  evaluated_count: number;
  skipped_count: number;
  context_relevancy: number;
  /** null：该类型下暂无已确认标准答案的用例。 */
  answer_correctness: number | null;
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
  /** null：标准答案缺失或待人工确认，未参与答案正确度计算。 */
  answer_correctness: number | null;
  error?: string | null;
  question_type: string;
  dataset_version: string;
  needs_review: boolean;
  /** 阶段十二：该用例实际使用的检索策略 */
  retrieval_strategy?: string | null;
  // 阶段十四：Evidence Gate 归档（Gate 关闭时为 null）
  gate_decision?: string | null;
  gate_version?: string | null;
  retry_strategy?: string | null;
  // 阶段十五：Self Reflection 归档（Reflection 关闭时为 null）
  reflection_decision?: string | null;
  reflection_version?: string | null;
  reflection_retry_strategy?: string | null;
  reflection_reason?: string | null;
}

/** 评估报告（POST /evaluation/run）。 */
export interface EvaluationReport {
  run_id: string;
  kb_id: string;
  case_count: number;
  context_relevancy: number;
  /** null：本次运行无已确认标准答案的用例。 */
  answer_correctness: number | null;
  passed: boolean;
  threshold: number;
  /** TASK-009 实验维度（Baseline 与消融实验对比）。 */
  experiment_name: string;
  retrieval_strategy: string;
  dataset_version: string;
  evaluated_count: number;
  skipped_count: number;
  by_question_type: QuestionTypeMetric[];
  results: EvalCaseResult[];
}

/** 实验运行归档（GET /evaluation/runs）。 */
export interface EvaluationRunItem {
  run_id: string;
  kb_id: string;
  experiment_name: string;
  retrieval_strategy: string;
  dataset_version: string;
  case_count: number;
  evaluated_count: number;
  skipped_count: number;
  context_relevancy: number;
  answer_correctness: number | null;
  passed: boolean;
  threshold: number;
  config_snapshot: Record<string, unknown>;
  by_question_type: QuestionTypeMetric[];
  created_at: string | null;
}

/** 历史评估结果条目（GET /evaluation/results）。 */
export interface EvaluationHistoryItem {
  id: string;
  question: string;
  golden_answer: string;
  answer_correctness: number | null;
  context_relevancy: number;
  created_at: string | null;
  run_id?: string | null;
  experiment_name?: string | null;
  retrieval_strategy?: string | null;
  dataset_version?: string | null;
  question_type?: string | null;
  question_type_label?: string | null;
  // 阶段十四：Evidence Gate 归档（Gate 关闭时为 null）
  gate_decision?: string | null;
  gate_version?: string | null;
  retry_strategy?: string | null;
  // 阶段十五：Self Reflection 归档（Reflection 关闭时为 null）
  reflection_decision?: string | null;
  reflection_version?: string | null;
  reflection_retry_strategy?: string | null;
  reflection_reason?: string | null;
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
