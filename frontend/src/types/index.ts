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
   * structured（结构化数据记录） / structured_group（关联数据记录），
   * 与后端 SOURCE_KIND_LABELS 一致。
   */
  source_kind?: 'document' | 'resource' | 'structured' | 'structured_group';
  /** 文档所属知识库（来源定位链接用；历史数据可能为空） */
  kb_id?: string | null;
  /** 证据等级：high / medium / insufficient */
  evidence_level?: EvidenceLevel;
  /** Resource 专属：资源细分类型 */
  resource_type?: ResourceType | null;
  resource_id?: string | null;
  resource_name?: string | null;
  // ── 第六阶段：结构化 / 关联数据来源追溯 ──
  /** 结构化文件（非关联）原始文档 ID；关联记录不使用该字段 */
  source_doc_id?: string | null;
  /** 普通文档的原始文档 ID（与 doc_id 同值，统一入口用） */
  document_id?: string | null;
  /** 结构化 / 关联知识记录 ID（一条记录一个向量） */
  record_id?: string | null;
  /** 记录定位：行号 / 工作表 / JSON 路径 / 关联记录序号 */
  locate?: string | null;
  /** 结构化记录字段与原始值（只读展示，后端已限量） */
  fields?: Record<string, unknown> | null;
  /** 关联数据文件组 ID / 名称 */
  group_id?: string | null;
  group_name?: string | null;
  /** 关联记录的参与文件（文件名 / 角色名 / 记录定位） */
  member_files?: { doc_id?: string; file_name?: string; alias?: string; locate?: string }[] | null;
  /** 关联记录使用的关系（字段对与关联类型） */
  relations?: { id?: string; from_field?: string; to_field?: string; kind?: string }[] | null;
  relation_ids?: string[] | null;
  // ── 阶段十统一 Evidence 访问入口 ──
  evidence_id?: string;
  source_id?: string | null;
  source_name?: string;
  /** 来源分类展示标签：文档 / 中药 / 方剂 / 理论 / 文献 */
  source_label?: string;
  evidence_text?: string;
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
  /**
   * 流式问答的当前阶段（仅发送中的 assistant 占位消息使用），
   * 全部由真实 SSE 事件驱动（不伪造状态）：
   * - analyzing：已发出问题，后端正在分析问题（Query Analyzer / 路由决策）；
   * - searching：start 事件已到，正在检索知识库（RAG 检索耗时较长）；
   * - generating：citations 事件已到（已找到 X 条证据），LLM 正在流式生成。
   * 生成结束（done/error）后移除；历史消息不携带该字段。
   */
  streamPhase?: 'analyzing' | 'searching' | 'generating';
  /** 阶段十：多来源证据（流式由 citations 事件下发；历史消息由 citations 推导） */
  evidence?: Evidence[];
  evidenceGroups?: EvidenceGroup[];
  evidenceSummary?: EvidenceSummary;
  /** 阶段十一：Query 分析（流式由 start 事件下发） */
  queryAnalysis?: QueryAnalysis;
  /** 阶段十二：检索策略路由决策（流式由 start 事件下发） */
  routerDecision?: RouterDecision;
  /** 阶段十四：证据门控决策（流式由 citations 事件下发） */
  evidenceGate?: GateDecision;
  /** 阶段十五：自反思决策（流式由 done 事件下发） */
  reflection?: ReflectionDecision;
  /** 流式回答耗时（秒）：done 时由前端记录（问题2：可折叠摘要） */
  streamDuration?: number;
  /** 流式开始时间戳（performance.now）：用于计算 streamDuration */
  streamStartTime?: number;
  /** 用户主动停止生成（保留已显示正文，UI 标记“已停止生成”） */
  cancelled?: boolean;
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

/**
 * 文件空间（与后端 src/core/file_spaces.py 一致）。
 *
 * - document：普通文档，进入解析 + 向量化（RAG 检索来源）
 * - structured：结构化数据，只做字段识别 + 记录生成（不切片、不 Embedding、不写 Milvus）
 *
 * 两个空间各自拥有一棵文件夹树，文件不会跨空间混在一起。
 */
export type FileSpace = 'document' | 'structured';

/**
 * pending=待解析 / parsing=解析中 / success=切片就绪 / completed=已向量化 /
 * failed=失败 / skipped=结构化数据文件（按设计不参与解析与向量化）。
 */
export type ParseStatus = 'pending' | 'parsing' | 'success' | 'completed' | 'failed' | 'skipped';

/** 文件夹树节点（GET /folders 返回的扁平列表，前端按 parent_id 组树）。 */
export interface FileFolderItem {
  id: string;
  space: FileSpace;
  name: string;
  /** null = 挂在空间根目录下 */
  parent_id: string | null;
  /** 该目录下的直接文件数（不递归） */
  document_count: number;
  /** 该目录下的直接子文件夹数（不递归） */
  child_count: number;
  created_at: string;
  updated_at: string;
}

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
  /** 替换文件/在线编辑后由后端更新（历史数据可能与 created_at 相同）。 */
  updated_at?: string;
  /** 所属文件空间；后端始终返回，缺省按普通文档处理。 */
  space?: FileSpace;
  /** 所在文件夹；null = 空间根目录。 */
  folder_id?: string | null;
  /**
   * 结构化解析状态（仅结构化数据空间的 CSV / TSV / XLSX / JSON 有值）。
   *
   * 与 parse_status 是两套进度：parse_status 表达「切片 + 向量化」（结构化文件恒为
   * skipped），structured_status 表达「字段识别 + 记录生成」，两者互不影响。
   */
  structured_status?: 'pending' | 'parsing' | 'success' | 'failed' | string | null;
  structured_record_count?: number | null;
  structured_field_count?: number | null;
  structured_error_message?: string | null;
  /** 结构化向量化进度（仅结构化空间列表返回）：pending / indexing / completed / failed */
  structured_vector_status?: string | null;
  /** 已写入 Milvus 的向量条数（配合 record_count 显示进度条） */
  structured_vector_count?: number | null;
  structured_vector_error?: string | null;
  /**
   * 向量索引的真实状态（统一处理状态用）：parse_status 只说明切片是否就绪，
   * 是否真的进了 Milvus 由这两列决定，列表因此能区分「切片就绪」与「已可检索」。
   */
  index_status?: 'not_indexed' | 'indexed' | 'stale' | 'failed' | string;
  vector_count?: number;
  /** 上传/替换响应才有：同库内内容 sha256 完全相同的其他文件名（最多 5 个，仅提示）。 */
  duplicate_names?: string[];
}

/** 结构化解析状态（与后端 structured_parses.status 一致）。 */
export type StructuredParseStatus = 'pending' | 'parsing' | 'success' | 'failed';

/** 识别出的一个字段：名称与类型都来自文件本身，后端不预设固定字段。 */
export interface StructuredField {
  name: string;
  /** integer / float / bool / date / percent / null / mixed / string */
  type: string;
  /** 该字段在本文件中的空值数 */
  null_count: number;
  /** 该字段被记录的次数（列数多于表头等兜底列可能为 0） */
  count: number;
  /** XLSX 多工作表时标注来源表 */
  sheet?: string | null;
  /** 补充说明，如「同一列出现多种数据类型」 */
  note?: string | null;
}

/** 一个文件的结构化解析概览（GET /structured/{doc_id}/parse）。 */
export interface StructuredParseInfo {
  doc_id: string;
  file_name: string;
  /** 所在文件夹链，如「古籍 / 本草 / 补益药」；根目录为空串 */
  folder_path: string;
  status: StructuredParseStatus | string;
  detected_format: string;
  encoding: string;
  fields: StructuredField[];
  field_count: number;
  record_count: number;
  /** 跳过的行数（全空行、超出上限等） */
  skipped_count: number;
  /** 含空值或异常提示的记录数 */
  anomaly_count: number;
  /** 记录数达到上限，后面未解析 */
  truncated: boolean;
  error_message: string;
  parsed_at?: string | null;
  /** 该格式是否属于结构化解析支持范围（csv/tsv/xlsx/json） */
  supported: boolean;
  /** 第六阶段：统一向量索引状态（pending / indexing / completed / failed） */
  vector_status: string;
  /** 已写入 Milvus 的向量条数（一条知识记录一个向量） */
  vector_count: number;
  /** 写入向量时使用的 Embedding 模型标识 */
  vector_model?: string | null;
  vector_error: string;
  vectorized_at?: string | null;
}

/** 预览列表中的一条知识记录（不含完整原始字段）。 */
export interface StructuredRecordItem {
  id: string;
  /** 原始行号（XLSX 为该工作表内行号，跨表可能重号） */
  row_no: number;
  sheet_name?: string | null;
  json_path?: string | null;
  /** JSON 对象按键展开时的键名 */
  record_label: string;
  /** 由实际字段拼成的独立知识记录文本 */
  knowledge_text: string;
  /** 空值与异常提示 */
  anomalies: string[];
  field_count: number;
  empty_field_count: number;
}

/** 记录分页结果：每次只取一页，避免大文件全量加载。 */
export interface StructuredRecordPage {
  items: StructuredRecordItem[];
  total: number;
  limit: number;
  offset: number;
  status: StructuredParseStatus | string;
  error_message: string;
}

/** 单条记录详情：额外带原始字段与值 + 来源追溯。 */
export interface StructuredRecordDetail extends StructuredRecordItem {
  doc_id: string;
  file_name: string;
  file_type: string;
  folder_path: string;
  payload: Record<string, unknown>;
  created_at?: string | null;
}

// ─────────────────── 关联结构化数据（第四阶段） ───────────────────
// 一个文件组 = 一个独立的关联导入任务；正式关联记录只在用户确认关系后生成。

/** 文件组状态：草稿 → 已校验 → 已确认；组内文件替换/删除后转 stale 需重新校验。 */
export type StructuredGroupStatus = 'draft' | 'validated' | 'confirmed' | 'stale';

/** 关联类型（由两侧字段的主键唯一性决定，不是靠字段名猜出来的）。 */
export type RelationKind = 'one_to_one' | 'one_to_many' | 'many_to_one' | 'many_to_many';

/** 组内一个文件：结构识别结果 + 主键候选（候选仍需用户确认）。 */
export interface StructuredGroupFile {
  doc_id: string;
  file_name: string;
  /** 文件当前的名字（重命名后与建组时快照不同） */
  current_file_name: string;
  alias: string;
  order_no: number;
  parse_status: StructuredParseStatus | string;
  detected_format: string;
  record_count: number;
  anomaly_count: number;
  error_message: string;
  fields: string[];
  field_specs: StructuredField[];
  key_candidates: { field: string; type: string; unique: boolean; score: number; reason: string }[];
  /** 文件已被删除或进了回收站 */
  removed: boolean;
}

export interface StructuredGroup {
  id: string;
  kb_id: string;
  name: string;
  status: StructuredGroupStatus | string;
  record_count: number;
  error_message: string;
  relation_count: number;
  /** 尚未经用户确认的关系条数（>0 时不能生成正式关联记录） */
  unconfirmed_count: number;
  file_count: number;
  created_at?: string | null;
  updated_at?: string | null;
  validated_at?: string | null;
  confirmed_at?: string | null;
  /** 第六阶段：统一向量索引状态（pending / indexing / completed / failed） */
  vector_status: string;
  vector_count: number;
  vector_model?: string | null;
  vector_error: string;
  vectorized_at?: string | null;
}

/** 一条关联关系：manual 是用户手动选字段；suggested 是系统推荐（需人工确认）。 */
export interface StructuredRelation {
  id: string;
  from_doc_id: string;
  from_field: string;
  to_doc_id: string;
  to_field: string;
  kind: RelationKind | string;
  origin: 'suggested' | 'manual' | string;
  confirmed: boolean;
  confidence?: number | null;
  coverage?: number;
  /** 0–100 规则打分（不是概率）；旧数据可能没有 */
  score?: number | null;
  /** 评分等级：high ≥90 / medium 70–89 / low <70 */
  tier?: 'high' | 'medium' | 'low' | string | null;
  row_coverage?: number;
  evidence?: string[];
  warnings?: string[];
  factors?: Record<string, number>;
  /** 历史字段：是否曾被自动预填（当前不再产生预填关系） */
  prefilled?: boolean;
  reason?: string;
}

/** 校验报告：问题清单 + 每条关系的匹配/未匹配/重复/孤立计数。 */
export interface StructuredGroupValidation {
  issues: { level: 'error' | 'warning' | 'info' | string; type: string; relation_id: string; message: string }[];
  relation_stats: Record<string, unknown>[];
  error_count: number;
  warning_count: number;
  unconfirmed_count: number;
  structural_error_count: number;
  unmatched_error_count: number;
  can_confirm: boolean;
  /** 存在外键无法匹配的记录：确认生成前需要用户明确知情（force） */
  force_required: boolean;
  total_rows: number;
  generation?: Record<string, unknown>;
  checked_at?: string;
}

export interface StructuredGroupDetail {
  group: StructuredGroup;
  files: StructuredGroupFile[];
  relations: StructuredRelation[];
  validation: Partial<StructuredGroupValidation>;
}

// ── 统一处理状态（第五阶段）──────────────────────────────────────────────────
// 三条链路（普通文档 / 非关联结构化 / 关联文件组）对外说同一套状态词汇，
// 各业务原有的库字段（parse_status / structured_parses.status / 组 status）保持不变。

/** 统一状态词汇；三条链路都不提供取消，因此没有 cancelled。 */
export type ProcessingStatus =
  | 'pending'
  | 'processing'
  | 'awaiting_confirm'
  | 'success'
  | 'failed';

export type ProcessingStageState = 'done' | 'active' | 'pending' | 'failed';

export interface ProcessingStage {
  index: number;
  label: string;
  state: ProcessingStageState;
}

/** 后端 src/utils/processing_status.py 的派生结果（只读，不含估算百分比）。 */
export interface ProcessingView {
  pipeline: 'document' | 'structured' | 'group' | string;
  status: ProcessingStatus | string;
  stage_index: number;
  stage_label: string;
  stage_total: number;
  stages: ProcessingStage[];
  reason: string;
  blocked_reason: string;
  retryable: boolean;
  retry_actions: string[];
  next_action: string;
  results: Record<string, unknown>;
}

/** GET /documents/{doc_id}/processing */
export interface DocumentProcessingInfo extends ProcessingView {
  doc_id: string;
  file_name: string;
  space: FileSpace | string;
  duplicate_names: string[];
  groups: {
    id: string;
    name: string;
    status: string;
    relation_count: number;
    unconfirmed_count: number;
    record_count: number;
    error_message: string;
  }[];
  updated_at?: string | null;
}

/** GET /structured/groups/{group_id}/processing */
export interface GroupProcessingInfo extends ProcessingView {
  group_id: string;
  name: string;
  members: {
    doc_id: string;
    file_name: string;
    alias: string;
    parse_status: string;
    record_count: number;
    error_message: string;
    removed: boolean;
  }[];
  updated_at?: string | null;
}

/** 一条关联知识记录（列表项，详情接口再取 parts / sources）。 */
export interface StructuredGroupRecordItem {
  id: string;
  seq_no: number;
  anchor_doc_id?: string | null;
  knowledge_text: string;
  anomaly_count: number;
  source_count: number;
  relation_count: number;
}

export interface StructuredGroupRecordPage {
  items: StructuredGroupRecordItem[];
  total: number;
  limit: number;
  offset: number;
  status: StructuredGroupStatus | string;
  record_count: number;
  error_message: string;
}

/** 关联记录详情：各参与文件的原始值 + 行号/工作表/JSON 路径 + 参与关联的字段。 */
export interface StructuredGroupRecordDetail extends StructuredGroupRecordItem {
  group_id: string;
  group_name: string;
  anchor_file: string;
  anchor_record_id?: string | null;
  parts: { doc_id: string; file_name: string; alias: string; locate: string; values: Record<string, unknown> }[];
  sources: {
    doc_id: string;
    file_name: string;
    alias: string;
    record_id: string;
    row_no?: number | null;
    sheet_name?: string | null;
    json_path?: string | null;
    join_field?: string | null;
    join_from_field?: string | null;
    role: string;
  }[];
  relation_ids: string[];
  anomalies: string[];
  created_at?: string | null;
}

/** 站内预览类型：决定弹窗内使用文本、表格还是专用渲染器。 */
export type DocumentPreviewKind =
  | 'text'
  | 'table'
  | 'json'
  | 'html'
  | 'docx'
  | 'xlsx'
  | 'pptx'
  | 'pdf'
  | 'image'
  | 'unsupported';

/** JSON 预览的树节点（与后端 build_json_tree 输出一致，供 AntD Tree 使用）。 */
export interface JsonTreeNode {
  key: string;
  title: string;
  children?: JsonTreeNode[];
}

export interface DocxBlock {
  type: 'heading' | 'paragraph';
  level?: number | null;
  text: string;
}

export interface XlsxSheet {
  name: string;
  rows: string[][];
}

export interface PptxSlide {
  index: number;
  lines: string[];
}

/** GET /documents/{id}/preview 返回的站内预览数据。 */
export interface DocumentPreview {
  id: string;
  file_name: string;
  file_type: string;
  file_size: number;
  parse_status: ParseStatus;
  kind: DocumentPreviewKind;
  editable: boolean;
  encoding?: string | null;
  content?: string | null;
  rows?: string[][] | null;
  delimiter?: string | null;
  blocks?: DocxBlock[] | null;
  sheets?: XlsxSheet[] | null;
  slides?: PptxSlide[] | null;
  /** kind=json：JSON 结构树（超限截断或非法时为 null） */
  tree?: JsonTreeNode[] | null;
  /** kind=html：服务端白名单清洗后的 HTML（仅用于 sandbox iframe 渲染） */
  html?: string | null;
  message?: string | null;
  truncated: boolean;
}

/** POST /documents/{id}/content 保存结果。 */
export interface SaveDocumentResult {
  id: string;
  file_size: number;
  encoding: string;
  parse_status: string;
  chunk_count: number;
  message: string;
}

/** POST /documents/{id}/replace 替换结果（文档 ID 不变）。 */
export interface ReplaceDocumentResult {
  id: string;
  file_name: string;
  file_size: number;
  parse_status: string;
  chunk_count: number;
  index_status: string;
  message: string;
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
