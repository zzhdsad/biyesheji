/**
 * 索引一致性前端工具测试：影响预览与环境门禁。
 *
 * 运行：npm test（node --test）
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import type { IndexDiffDocument, IndexDiffReport, IndexEnvironment } from '@/services/api';
import {
  buildFixPreview,
  buildRebuildPreview,
  canDeleteVectors,
  documentIssueTags,
  environmentMatches,
  indexStatusLabel,
} from './indexConsistency.ts';

function doc(overrides: Partial<IndexDiffDocument>): IndexDiffDocument {
  return {
    doc_id: 'doc',
    file_name: 'a.pdf',
    expected_chunks: 2,
    actual_vectors: 2,
    missing_count: 0,
    orphan_count: 0,
    duplicate_count: 0,
    missing_chunk_ids: [],
    orphan_vector_ids: [],
    duplicate_vector_ids: [],
    current_status: 'indexed',
    suggested_status: 'indexed',
    flag_inaccurate: false,
    ...overrides,
  };
}

const REPORT: IndexDiffReport = {
  kb_id: 'kb-1',
  scanned_documents: 3,
  milvus_available: true,
  error: '',
  clean: false,
  totals: {
    missing_vectors: 2,
    orphan_vectors: 3,
    duplicate_vectors: 1,
    inaccurate_flags: 2,
  },
  documents: [
    doc({ doc_id: 'a', missing_count: 2, missing_chunk_ids: ['c1', 'c2'] }),
    doc({
      doc_id: 'b',
      orphan_count: 3,
      duplicate_count: 1,
      flag_inaccurate: true,
      current_status: 'indexed',
      suggested_status: 'stale',
    }),
    doc({ doc_id: 'c' }),
  ],
};

const DEV_ENV: IndexEnvironment = {
  env: 'dev',
  database: 'kp_dev',
  milvus_collection: 'document_chunks_dev',
  isolated_collection: true,
  destructive_allowed: true,
};

describe('修复影响预览', () => {
  it('只修正状态标记时不需要二次确认', () => {
    const preview = buildFixPreview(REPORT, {
      fix_flags: true,
      delete_orphans: false,
      delete_duplicates: false,
    });
    assert.equal(preview.flags_to_update, 2);
    assert.equal(preview.orphan_vectors, 0);
    assert.equal(preview.duplicate_vectors, 0);
    assert.equal(preview.requires_confirmation, false);
  });

  it('选择删除孤儿/重复向量时必须要求二次确认', () => {
    const preview = buildFixPreview(REPORT, {
      fix_flags: true,
      delete_orphans: true,
      delete_duplicates: true,
    });
    assert.equal(preview.orphan_vectors, 3);
    assert.equal(preview.duplicate_vectors, 1);
    assert.equal(preview.requires_confirmation, true);
    assert.ok(preview.summary.some((line) => line.includes('不可恢复')));
  });

  it('默认不删除任何向量（所有删除开关关闭）', () => {
    const preview = buildFixPreview(REPORT, {
      fix_flags: true,
      delete_orphans: false,
      delete_duplicates: false,
    });
    assert.equal(preview.orphan_vectors, 0);
    assert.equal(preview.duplicate_vectors, 0);
  });

  it('受影响文档数按任一类问题统计', () => {
    const preview = buildFixPreview(REPORT, {
      fix_flags: true,
      delete_orphans: false,
      delete_duplicates: false,
    });
    assert.equal(preview.affected_documents, 2);
  });
});

describe('删除向量环境门禁', () => {
  it('隔离集合且非生产时允许删除', () => {
    assert.equal(canDeleteVectors(DEV_ENV), true);
  });

  it('正式集合一律禁止删除', () => {
    assert.equal(
      canDeleteVectors({ ...DEV_ENV, milvus_collection: 'document_chunks', isolated_collection: false, destructive_allowed: false }),
      false,
    );
  });

  it('环境信息缺失时 fail-closed', () => {
    assert.equal(canDeleteVectors(null), false);
  });

  it('前端绑定集合与后端不一致时判定不匹配', () => {
    assert.equal(environmentMatches(DEV_ENV, 'document_chunks_dev'), true);
    assert.equal(environmentMatches(DEV_ENV, 'document_chunks_prod'), false);
    assert.equal(environmentMatches(null, 'document_chunks_dev'), false);
  });
});

describe('重建预览与状态文案', () => {
  it('重建只统计缺失向量与失败文档，不包含删除', () => {
    const preview = buildRebuildPreview(REPORT);
    assert.equal(preview.documents_with_missing, 1);
    assert.equal(preview.documents_with_errors, 0);
    assert.ok(preview.summary.some((line) => line.includes('不会删除任何向量')));
  });

  it('状态与问题标签可读', () => {
    assert.equal(indexStatusLabel('stale'), '部分缺失');
    assert.equal(indexStatusLabel('custom'), 'custom');
    assert.deepEqual(
      documentIssueTags(
        doc({ missing_count: 1, orphan_count: 2, duplicate_count: 3, flag_inaccurate: true }),
      ),
      ['缺失 1', '孤儿 2', '重复 3', '状态异常'],
    );
  });
});
