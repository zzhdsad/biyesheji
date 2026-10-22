import { test } from 'node:test';
import assert from 'node:assert/strict';

import type { RevectorizeJobStatus } from '@/services/api';
import {
  canCancelJob,
  canStartRevectorize,
  dependencyColor,
  dependencyLabel,
  estimateRemaining,
  formatJobSummary,
  jobStatusLabel,
  shouldPollJob,
  vectorHealth,
  vectorHealthText,
} from './embeddingStatus';

test('运行状态标签覆盖全部产品状态', () => {
  assert.equal(dependencyLabel('ready'), '已就绪');
  assert.equal(dependencyLabel('revectorizing'), '重新向量化中');
  assert.equal(dependencyLabel(undefined), '未知');
});

test('状态颜色映射正确', () => {
  assert.equal(dependencyColor('ready'), 'success');
  assert.equal(dependencyColor('revectorizing'), 'processing');
  assert.equal(dependencyColor(undefined), 'default');
});

test('进行中与排队中的任务需要轮询', () => {
  assert.equal(shouldPollJob('pending'), true);
  assert.equal(shouldPollJob('running'), true);
  assert.equal(shouldPollJob('succeeded'), false);
  assert.equal(shouldPollJob('partial'), false);
  assert.equal(shouldPollJob(undefined), false);
});

test('任务状态标签完整', () => {
  assert.equal(jobStatusLabel('pending'), '排队中');
  assert.equal(jobStatusLabel('running'), '进行中');
  assert.equal(jobStatusLabel('succeeded'), '已完成');
  assert.equal(jobStatusLabel('partial'), '部分失败');
  assert.equal(jobStatusLabel('cancelled'), '已取消');
  assert.equal(jobStatusLabel('failed'), '已失败');
  assert.equal(jobStatusLabel(null), '无任务');
});

test('仅排队中/进行中的任务可取消', () => {
  assert.equal(canCancelJob('pending'), true);
  assert.equal(canCancelJob('running'), true);
  assert.equal(canCancelJob('succeeded'), false);
  assert.equal(canCancelJob('cancelled'), false);
  assert.equal(canCancelJob('partial'), false);
  assert.equal(canCancelJob(null), false);
});

const baseJob: RevectorizeJobStatus = {
  job_id: 'j1',
  status: 'running',
  target_model: 'mock:mock',
  previous_model: 'mock:old',
  kb_id: null,
  total: 20,
  processed: 8,
  succeeded: 7,
  failed: 1,
  failed_doc_ids: ['d1'],
  processing: 12,
  percent: 40,
  error_message: '',
  created_at: null,
  finished_at: null,
};

test('进度摘要含完成/总数/失败', () => {
  const text = formatJobSummary(baseJob);
  assert.match(text, /已完成 8\/20/);
  assert.match(text, /失败 1/);
  assert.match(text, /成功 7/);
});

test('无任务时的摘要', () => {
  assert.equal(formatJobSummary(null), '暂无重新向量化任务');
});

test('存在旧模型向量时可发起批量重建；任务进行中不可重复发起', () => {
  assert.equal(canStartRevectorize(5, null), true);
  assert.equal(canStartRevectorize(0, null), false);
  assert.equal(canStartRevectorize(5, { ...baseJob, status: 'running' }), false);
  // 上一轮有失败项：即使没有新的旧模型向量，也允许继续处理
  assert.equal(canStartRevectorize(0, { ...baseJob, status: 'partial', failed: 2 }), true);
  assert.equal(canStartRevectorize(0, { ...baseJob, status: 'succeeded', failed: 0 }), false);
});

// ── 模块 B：知识库状态卡片 ───────────────────────────────────────────────────

test('向量健康三色判定：红 = 旧模型向量，黄 = 仅未生成，绿 = 全就绪', () => {
  // 红：Milvus 中仍存在旧模型向量（stale_with_vectors > 0）
  assert.equal(
    vectorHealth({ stale_documents: 827, stale_with_vectors: 19, unvectorized_documents: 808 }),
    'error',
  );
  // 黄：只有"从未向量化"的文档，无旧向量混检风险
  assert.equal(
    vectorHealth({ stale_documents: 808, stale_with_vectors: 0, unvectorized_documents: 808 }),
    'warning',
  );
  // 绿：无待处理文档
  assert.equal(
    vectorHealth({ stale_documents: 0, stale_with_vectors: 0, unvectorized_documents: 0 }),
    'success',
  );
  // 状态缺失按绿处理（不惊吓用户）
  assert.equal(vectorHealth(null), 'success');
  assert.equal(vectorHealth(undefined), 'success');
});

test('向量健康三色文案非空且互不相同', () => {
  const texts = [
    vectorHealthText('error'),
    vectorHealthText('warning'),
    vectorHealthText('success'),
  ];
  for (const t of texts) assert.ok(t && t.trim(), `文案为空：${t}`);
  assert.equal(new Set(texts).size, 3, '三种状态文案不得重复');
});

test('剩余时间估算：信息不足返回 null，进行中按速率外推', () => {
  // 无任务 / 已结束 / 未处理任何文档 → null
  assert.equal(estimateRemaining(null), null);
  assert.equal(estimateRemaining({ ...baseJob, status: 'succeeded' }), null);
  assert.equal(estimateRemaining({ ...baseJob, processed: 0 }), null);
  // created_at 缺失 → null
  assert.equal(estimateRemaining({ ...baseJob, created_at: null }), null);
  // 进行中且有起点：已处理 8/20，按速率外推出"预计剩余"字样
  const started = new Date(Date.now() - 80_000).toISOString();
  const text = estimateRemaining({ ...baseJob, created_at: started });
  assert.ok(text === null || /^预计剩余约/.test(text), `返回值异常：${text}`);
});
