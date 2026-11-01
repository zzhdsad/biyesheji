/**
 * 索引一致性的前端纯函数：影响范围预览、环境门禁与状态文案。
 *
 * 约束：删除向量是不可逆操作，前端只负责"显示影响 + 阻止误操作"，
 * 最终安全边界仍由后端（confirmed=True + 隔离集合）兜底。
 */
import type {
  IndexDiffDocument,
  IndexDiffReport,
  IndexEnvironment,
} from '@/services/api';

export interface IndexFixPlan {
  fix_flags: boolean;
  delete_orphans: boolean;
  delete_duplicates: boolean;
}

export interface IndexFixPreview {
  scanned_documents: number;
  affected_documents: number;
  flags_to_update: number;
  orphan_vectors: number;
  duplicate_vectors: number;
  missing_vectors: number;
  requires_confirmation: boolean;
  summary: string[];
}

function docsWithIssues(report: IndexDiffReport): IndexDiffDocument[] {
  return report.documents.filter(
    (doc) =>
      doc.flag_inaccurate ||
      doc.missing_count > 0 ||
      doc.orphan_count > 0 ||
      doc.duplicate_count > 0,
  );
}

/** 修复操作的影响范围预览（只读，不触发任何请求）。 */
export function buildFixPreview(report: IndexDiffReport, plan: IndexFixPlan): IndexFixPreview {
  const affected = docsWithIssues(report);
  const flags = plan.fix_flags ? report.totals.inaccurate_flags : 0;
  const orphans = plan.delete_orphans ? report.totals.orphan_vectors : 0;
  const duplicates = plan.delete_duplicates ? report.totals.duplicate_vectors : 0;
  const summary: string[] = [];
  if (plan.fix_flags) summary.push(`修正 ${report.totals.inaccurate_flags} 个文档的索引状态标记`);
  if (plan.delete_orphans) summary.push(`删除 ${report.totals.orphan_vectors} 条孤儿向量`);
  if (plan.delete_duplicates) summary.push(`删除 ${report.totals.duplicate_vectors} 条重复向量`);
  if (plan.delete_orphans || plan.delete_duplicates) {
    summary.push('删除向量不可恢复：仅影响孤儿/重复向量，不会删除文档记录');
  }
  if (summary.length === 0) summary.push('当前未选择任何修复操作');

  return {
    scanned_documents: report.scanned_documents,
    affected_documents: affected.length,
    flags_to_update: flags,
    orphan_vectors: orphans,
    duplicate_vectors: duplicates,
    missing_vectors: report.totals.missing_vectors,
    requires_confirmation: orphans > 0 || duplicates > 0,
    summary,
  };
}

/** 删除向量前的环境门禁：非隔离集合或生产环境一律不允许。 */
export function canDeleteVectors(env: IndexEnvironment | null): boolean {
  return Boolean(env?.isolated_collection && env?.destructive_allowed);
}

/** 前端绑定的集合必须与后端当前集合一致，否则应刷新而不是继续操作。 */
export function environmentMatches(
  env: IndexEnvironment | null,
  expectedCollection?: string,
): boolean {
  if (!env) return false;
  if (!expectedCollection) return true;
  return env.milvus_collection === expectedCollection;
}

export interface RebuildPreview {
  documents_with_missing: number;
  documents_with_errors: number;
  summary: string[];
}

/** 重建/重试的影响范围：只重置状态，由既有向量化流程重新入库。 */
export function buildRebuildPreview(report: IndexDiffReport): RebuildPreview {
  const missing = report.documents.filter((doc) => doc.missing_count > 0);
  const failed = report.documents.filter((doc) => doc.current_status === 'failed');
  return {
    documents_with_missing: missing.length,
    documents_with_errors: failed.length,
    summary: [
      `将 ${missing.length} 个存在缺失向量的文档重置为「未索引」`,
      `将 ${failed.length} 个失败文档的错误信息清空并等待重试`,
      '不会删除任何向量，重建由既有向量化任务执行',
    ],
  };
}

const STATUS_LABEL: Record<string, string> = {
  not_indexed: '未索引',
  indexing: '索引中',
  indexed: '已索引',
  failed: '索引失败',
  stale: '部分缺失',
};

export function indexStatusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

/** 文档问题的可读标签（表格展示用）。 */
export function documentIssueTags(doc: IndexDiffDocument): string[] {
  const tags: string[] = [];
  if (doc.missing_count > 0) tags.push(`缺失 ${doc.missing_count}`);
  if (doc.orphan_count > 0) tags.push(`孤儿 ${doc.orphan_count}`);
  if (doc.duplicate_count > 0) tags.push(`重复 ${doc.duplicate_count}`);
  if (doc.flag_inaccurate) tags.push('状态异常');
  return tags;
}
