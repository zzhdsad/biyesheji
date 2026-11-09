import type {
  DocumentItem,
  DocumentProcessingInfo,
  GroupProcessingInfo,
  ProcessingStatus,
  ProcessingView,
  StructuredGroup,
} from '@/types';

/**
 * 统一处理状态展示工具（纯函数，便于单测）。
 *
 * 词汇表与后端 `src/utils/processing_status.py` 一一对应：
 * 待处理 / 处理中 / 待用户确认 / 已完成 / 处理失败。
 * 各业务自己的库字段（parse_status、structured_parses.status、文件组 status）
 * 仍然保留，这里只负责把它们翻译成界面上的同一句话。
 *
 * 进度只有「第几步 / 共几步」这种由真实状态推出来的表述，没有估算百分比。
 */

export const PROCESSING_STATUS_META: Record<ProcessingStatus, { label: string; color: string }> = {
  pending: { label: '待处理', color: 'default' },
  processing: { label: '处理中', color: 'processing' },
  awaiting_confirm: { label: '待用户确认', color: 'gold' },
  success: { label: '已完成', color: 'success' },
  failed: { label: '处理失败', color: 'error' },
};

const UNKNOWN_STATUS_META = { label: '未知', color: 'default' };

export function processingStatusMeta(status: string | null | undefined): { label: string; color: string } {
  const known = status ? PROCESSING_STATUS_META[status as ProcessingStatus] : undefined;
  return known ?? { label: status || UNKNOWN_STATUS_META.label, color: UNKNOWN_STATUS_META.color };
}

export function processingStatusLabel(status: string | null | undefined): string {
  return processingStatusMeta(status).label;
}

export function processingStatusColor(status: string | null | undefined): string {
  return processingStatusMeta(status).color;
}

/** 向量索引状态 → 展示文案（与后端 VECTOR_STATUS_* 一一对应，如实展示不估算）。 */
export const VECTOR_STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: '待向量化', color: 'default' },
  indexing: { label: '向量化中', color: 'processing' },
  completed: { label: '已向量化', color: 'success' },
  failed: { label: '向量化失败', color: 'error' },
};

export function vectorStatusMeta(status: string | null | undefined): {
  label: string;
  color: string;
} {
  const known = status ? VECTOR_STATUS_META[status] : undefined;
  return known ?? { label: status || '未知', color: 'default' };
}

export function vectorStatusLabel(status: string | null | undefined): string {
  return vectorStatusMeta(status).label;
}

/** 统一状态 → AntD Alert/Result 的 tone（供处理概览面板用，避免各处自己判断）。 */
export function processingAlertType(
  status: string | null | undefined,
): 'error' | 'warning' | 'info' | 'success' {
  switch (status) {
    case 'failed':
      return 'error';
    case 'awaiting_confirm':
      return 'warning';
    case 'success':
      return 'success';
    default:
      return 'info';
  }
}

/** 下一步动作 → 中文（后端 next_action / retry_actions 的取值集合）。 */
export const NEXT_ACTION_LABELS: Record<string, string> = {
  reparse: '重新解析',
  revectorize: '重新向量化',
  structured_reparse: '重新识别字段',
  reparse_members: '重新解析组内文件',
  add_files: '补充文件',
  manual_relations: '手动配置关联字段',
  confirm_relations: '确认关联关系',
  validate: '运行数据校验',
  generate: '确认并生成关联记录',
  regenerate: '重新生成关联记录',
  none: '',
};

export function nextActionLabel(action: string | null | undefined): string {
  if (!action) return '';
  return NEXT_ACTION_LABELS[action] ?? '';
}

/** 阶段进度：「文本切片（第 3/5 步）」；已完成时直接说「全部完成」。 */
export function formatStageProgress(view: ProcessingView | null | undefined): string {
  if (!view) return '';
  if (view.status === 'success') return `${view.stage_total} 步全部完成`;
  const index = Math.min(Math.max(view.stage_index + 1, 1), view.stage_total);
  return `${view.stage_label}（第 ${index}/${view.stage_total} 步）`;
}

/**
 * 一句话摘要：状态 + 阶段 + 原因。
 * 原因（reason / blocked_reason）来自后端真实字段，不做任何润色或猜测。
 */
export function processingSummary(view: ProcessingView | null | undefined): string {
  if (!view) return '';
  const parts = [processingStatusLabel(view.status), formatStageProgress(view)];
  const detail = view.blocked_reason || view.reason;
  if (detail) parts.push(detail);
  return parts.filter(Boolean).join(' · ');
}

/** 是否需要继续轮询：只有「处理中」才轮询，其余状态由用户操作驱动。 */
export function shouldPollProcessing(view: ProcessingView | null | undefined): boolean {
  return view?.status === 'processing';
}

/**
 * 后台任务是否「已排队或正在跑」（pending 表示已派发但还没开始，同样要刷新）。
 * 列表与抽屉的轮询判定用这一个函数，避免各处再手写字符串比较。
 */
export function isTaskInFlight(status: string | null | undefined): boolean {
  return status === 'pending' || status === 'parsing';
}

/** 结果计数摘要：把 results 里已落库的条数念出来，供追溯「产生了什么结果」。 */
export function formatResultCounts(view: ProcessingView | null | undefined): string {
  if (!view) return '';
  const r = view.results as Record<string, unknown>;
  const num = (key: string): number | null => {
    const value = r?.[key];
    return typeof value === 'number' ? value : null;
  };
  const parts: string[] = [];
  const push = (label: string, key: string) => {
    const value = num(key);
    if (value !== null) parts.push(`${label} ${value}`);
  };
  if (view.pipeline === 'document') {
    push('切片', 'chunk_count');
    push('向量', 'vector_count');
  } else if (view.pipeline === 'structured') {
    push('字段', 'field_count');
    push('记录', 'record_count');
    const anomaly = num('anomaly_count');
    if (anomaly) parts.push(`异常提示 ${anomaly}`);
    if (r?.truncated) parts.push('已截断');
    push('向量', 'vector_count');
  } else {
    push('文件', 'usable_file_count');
    push('关系', 'relation_count');
    const unconfirmed = num('unconfirmed_count');
    if (unconfirmed) parts.push(`未确认 ${unconfirmed}`);
    push('关联记录', 'record_count');
    push('向量', 'vector_count');
  }
  return parts.join(' / ');
}

/**
 * 列表行 → 统一状态（与后端派生的粗口径一致，细粒度阶段仍由 processing 接口给出）。
 *
 * 普通文档：切片就绪但向量没进 Milvus，仍然算「待处理」——
 * 这是历史上最容易误判的地方（parse_status=success 并不等于可检索）。
 */
export function documentRowStatus(row: DocumentItem): ProcessingStatus {
  if (row.space === 'structured') return structuredRowStatus(row);
  switch (row.parse_status) {
    case 'parsing':
      return 'processing';
    case 'failed':
      return 'failed';
    case 'success':
      return row.index_status === 'failed' ? 'failed' : 'pending';
    case 'completed':
      if (row.index_status === 'failed') return 'failed';
      if (row.index_status === 'stale') return 'pending';
      if ((row.vector_count ?? 0) > 0 && row.vector_count !== row.chunk_count) return 'pending';
      return 'success';
    case 'skipped':
      return 'pending';
    default:
      return 'pending';
  }
}

/** 结构化空间行 → 统一状态（用 structured_status，而不是恒为 skipped 的 parse_status）。
 *
 * 解析成功不等于可检索：向量化现在由用户手动触发，因此还要看
 * structured_vector_status —— 已完成才算「已完成」，排队/进行中算「处理中」，
 * 未开始算「待处理」，失败算「处理失败」。
 */
export function structuredRowStatus(row: DocumentItem): ProcessingStatus {
  switch (row.structured_status) {
    case 'parsing':
      return 'processing';
    case 'failed':
      return 'failed';
    case 'success': {
      if ((row.structured_record_count ?? 0) === 0) return 'pending';
      switch (row.structured_vector_status) {
        case 'completed':
          return 'success';
        case 'indexing':
          return 'processing';
        case 'failed':
          return 'failed';
        case 'pending':
          return 'pending';
        case undefined:
        case null:
          return 'success'; // 兼容未返回向量状态的旧接口
        default:
          return 'pending';
      }
    }
    default:
      return 'pending';
  }
}

/**
 * 文件组成员的解析状态 → 统一状态。
 * 与 `structuredRowStatus` 同一口径：解析成功但没有记录仍算「待处理」。
 */
export function memberParseStatus(
  status: string | null | undefined,
  recordCount?: number | null,
): ProcessingStatus {
  switch (status) {
    case 'parsing':
      return 'processing';
    case 'failed':
      return 'failed';
    case 'success':
      return (recordCount ?? 0) > 0 ? 'success' : 'pending';
    default:
      return 'pending';
  }
}

/**
 * 文件组行 → 统一状态（列表里没有逐组的阶段详情，只能按库字段粗映射）。
 * 抽屉打开后用 processing 接口给出精确阶段与原因。
 */
export function groupRowStatus(group: StructuredGroup): ProcessingStatus {
  switch (group.status) {
    case 'confirmed': {
      if (group.record_count === 0) return 'pending';
      // 与结构化文件同一口径：确认生成记录后还要完成向量化才算「已完成」
      switch (group.vector_status) {
        case 'completed':
          return 'success';
        case 'indexing':
          return 'processing';
        case 'failed':
          return 'failed';
        default:
          return 'pending';
      }
    }
    case 'validated':
      return 'awaiting_confirm';
    case 'stale':
      return 'pending';
    default:
      return group.unconfirmed_count > 0 ? 'awaiting_confirm' : 'pending';
  }
}

export type ProcessingInfo = DocumentProcessingInfo | GroupProcessingInfo;
