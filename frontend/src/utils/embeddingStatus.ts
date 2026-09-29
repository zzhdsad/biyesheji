import type { EmbeddingDependencyState, RevectorizeJobStatus } from '@/services/api';

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
