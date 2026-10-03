import type {
  EmbeddingDependencyState,
  EmbeddingStatus,
  RevectorizeJobStatus,
} from '@/services/api';

/**
 * Embedding 状态展示工具（纯函数，便于单测）。
 *
 * 状态语义（与后端 `GET /settings/model/embedding/status` 对齐）：
 * - ready：模型可用（依赖与模型权重随镜像内置）
 * - revectorizing：批量重新向量化进行中
 */

/** 运行状态 → 中文标签。 */
export function dependencyLabel(state: EmbeddingDependencyState | undefined | null): string {
  switch (state) {
    case 'ready':
      return '已就绪';
    case 'revectorizing':
      return '重新向量化中';
    default:
      return '未知';
  }
}

/** 运行状态 → Tag 颜色（antd preset color）。 */
export function dependencyColor(
  state: EmbeddingDependencyState | undefined | null,
): 'success' | 'processing' | 'default' {
  switch (state) {
    case 'ready':
      return 'success';
    case 'revectorizing':
      return 'processing';
    default:
      return 'default';
  }
}

/** 任务状态 → 中文标签。 */
export function jobStatusLabel(status: RevectorizeJobStatus['status'] | undefined | null): string {
  switch (status) {
    case 'pending':
      return '排队中';
    case 'running':
      return '进行中';
    case 'succeeded':
      return '已完成';
    case 'partial':
      return '部分失败';
    case 'cancelled':
      return '已取消';
    case 'failed':
      return '已失败';
    default:
      return '无任务';
  }
}

/** 是否可取消（仅排队中 / 进行中的任务可取消）。 */
export function canCancelJob(status: RevectorizeJobStatus['status'] | undefined | null): boolean {
  return shouldPollJob(status);
}

/** 是否需要轮询任务进度（进行中或排队中）。 */
export function shouldPollJob(status: RevectorizeJobStatus['status'] | undefined | null): boolean {
  return status === 'pending' || status === 'running';
}

/** 进度摘要文案：已完成 / 总数，失败数。 */
export function formatJobSummary(job: RevectorizeJobStatus | null | undefined): string {
  if (!job) return '暂无重新向量化任务';
  const parts = [`已完成 ${job.processed}/${job.total}`];
  if (job.failed > 0) parts.push(`失败 ${job.failed}`);
  if (job.succeeded > 0) parts.push(`成功 ${job.succeeded}`);
  return parts.join('，');
}

/** 是否展示"批量重新向量化"入口（存在旧模型向量或任务未成功结束）。 */
export function canStartRevectorize(
  staleDocuments: number,
  job: RevectorizeJobStatus | null | undefined,
): boolean {
  if (shouldPollJob(job?.status)) return false;
  if (staleDocuments > 0) return true;
  // 上一轮有失败项时可继续重试
  return Boolean(job && job.failed > 0);
}

// ── 模块 B：知识库状态卡片（问题2）──────────────────────────────────────────

/** 向量健康总状态：红 = 存在旧模型向量；黄 = 部分文档等待生成；绿 = 全部就绪。 */
export type VectorHealth = 'error' | 'warning' | 'success';

/**
 * 三色状态判定（口径与后端一致，不做任何业务决策）：
 * - error（红）：stale_with_vectors > 0 —— Milvus 中仍存在旧模型向量，
 *   检索时被排除，这些文档需要重新生成向量后才能恢复参与检索；
 * - warning（黄）：仅 unvectorized_documents > 0 —— 从未真正向量化
 *   （检索天然不命中，不会产生错误结果），等待生成即可；
 * - success（绿）：stale_documents === 0 —— 无待处理文档。
 */
export function vectorHealth(
  vectors: Pick<
    EmbeddingStatus['vectors'],
    'stale_documents' | 'stale_with_vectors' | 'unvectorized_documents'
  > | null | undefined,
): VectorHealth {
  if (!vectors) return 'success';
  if ((vectors.stale_with_vectors ?? 0) > 0) return 'error';
  if ((vectors.stale_documents ?? 0) > 0 || (vectors.unvectorized_documents ?? 0) > 0) {
    return 'warning';
  }
  return 'success';
}

/** 三色状态 → 状态一句话文案（Antd Alert/Badge type 同名）。 */
export function vectorHealthText(health: VectorHealth): string {
  switch (health) {
    case 'error':
      return '存在旧模型向量，需要重新生成';
    case 'warning':
      return '部分文档等待生成向量';
    default:
      return '全部文档已完成向量化';
  }
}

/** 任务进行中 → 基于已处理进度估算剩余时间；信息不足返回 null。 */
export function estimateRemaining(job: RevectorizeJobStatus | null | undefined): string | null {
  if (!job || !shouldPollJob(job.status) || job.processed <= 0) return null;
  const startedAt = job.created_at ? Date.parse(job.created_at) : NaN;
  if (!Number.isFinite(startedAt)) return null;
  const elapsedMs = Date.now() - startedAt;
  if (elapsedMs <= 0) return null;
  const remainingDocs = Math.max(0, job.total - job.processed);
  const msPerDoc = elapsedMs / job.processed;
  const remainingMs = remainingDocs * msPerDoc;
  if (!Number.isFinite(remainingMs) || remainingMs < 1000) return null;
  const minutes = Math.floor(remainingMs / 60_000);
  const seconds = Math.round((remainingMs % 60_000) / 1000);
  return minutes > 0 ? `预计剩余约 ${minutes} 分钟` : `预计剩余约 ${seconds} 秒`;
}
