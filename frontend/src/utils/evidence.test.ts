/**
 * 阶段十：前端多源证据逻辑测试。
 *
 * 运行：npm run test:evidence（node --test，无需额外测试框架）
 *
 * 覆盖：证据等级规则、Document / Resource 归一化、四类资源分组、
 *       Document + Resource 混合分组、同来源聚合、分组排序、汇总计数。
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  buildEvidenceGroups,
  evidenceLevel,
  normalizeEvidence,
  summarizeEvidence,
} from './evidence.ts';
import type { Citation } from '@/types';

function docCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    chunk_id: 'chunk-1',
    source_index: 1,
    doc_id: 'doc-1',
    doc_name: '伤寒论.pdf',
    page_num: 5,
    title_path: '太阳病篇',
    content: '太阳病，头痛发热',
    score: 0.85,
    source_kind: 'document',
    ...overrides,
  };
}

function resourceCitation(
  overrides: Partial<Citation> & { resource_type?: string } = {},
): Citation {
  return {
    chunk_id: 'vec-1',
    source_index: 2,
    doc_id: 'a'.repeat(64),
    doc_name: '金银花',
    content: '金银花，甘寒，清热解毒',
    score: 0.78,
    source_kind: 'resource',
    resource_type: 'herb',
    resource_id: 'herb-1',
    resource_name: '金银花',
    ...overrides,
  } as Citation;
}

describe('evidenceLevel', () => {
  it('沿用既有分级规则 0.7 / 0.3', () => {
    assert.equal(evidenceLevel(0.95), 'high');
    assert.equal(evidenceLevel(0.7), 'high');
    assert.equal(evidenceLevel(0.69), 'medium');
    assert.equal(evidenceLevel(0.3), 'medium');
    assert.equal(evidenceLevel(0.29), 'insufficient');
  });
});

describe('normalizeEvidence', () => {
  it('Document：补齐统一 Evidence 字段', () => {
    const ev = normalizeEvidence(docCitation());
    assert.equal(ev.source_kind, 'document');
    assert.equal(ev.source_id, 'doc-1');
    assert.equal(ev.source_name, '伤寒论.pdf');
    assert.equal(ev.source_label, '文档');
    assert.equal(ev.evidence_text, '太阳病，头痛发热');
    assert.equal(ev.evidence_level, 'high');
    assert.equal(ev.resource_type, null);
  });

  it('Resource：source_id/name 取资源身份，标签为资源类型', () => {
    const ev = normalizeEvidence(resourceCitation());
    assert.equal(ev.source_kind, 'resource');
    assert.equal(ev.source_id, 'herb-1');
    assert.equal(ev.source_name, '金银花');
    assert.equal(ev.source_label, '中药');
    assert.equal(ev.evidence_level, 'high');
  });

  it('历史消息（缺 source_kind 等字段）按 Document 归一化', () => {
    const legacy: Citation = {
      chunk_id: 'c-1',
      source_index: 1,
      doc_id: 'd-1',
      doc_name: '旧文档.pdf',
      content: '旧内容',
      score: 0.5,
    };
    const ev = normalizeEvidence(legacy);
    assert.equal(ev.source_kind, 'document');
    assert.equal(ev.source_name, '旧文档.pdf');
    assert.equal(ev.evidence_level, 'medium');
    assert.equal(ev.evidence_id, 'c-1');
  });
});

describe('buildEvidenceGroups', () => {
  it('Document + Resource 混合 → 两组区分展示', () => {
    const groups = buildEvidenceGroups([docCitation(), resourceCitation()]);
    assert.deepEqual(
      groups.map((g) => g.group_key),
      ['document', 'resource:herb'],
    );
    assert.equal(groups[0].source_label, '文档');
    assert.equal(groups[1].source_label, '中药');
    assert.equal(groups[1].sources[0].source_name, '金银花');
  });

  it('四类资源 → 四个独立分组', () => {
    const groups = buildEvidenceGroups([
      resourceCitation({ resource_type: 'herb', resource_name: '金银花', score: 0.8 }),
      resourceCitation({ resource_type: 'prescription', resource_name: '银翘散', score: 0.7 }),
      resourceCitation({ resource_type: 'theory', resource_name: '辛凉解表', score: 0.6 }),
      resourceCitation({ resource_type: 'literature', resource_name: '温病条辨', score: 0.5 }),
    ]);
    assert.deepEqual(
      groups.map((g) => g.group_key),
      ['resource:herb', 'resource:prescription', 'resource:theory', 'resource:literature'],
    );
    assert.deepEqual(
      groups.map((g) => g.source_label),
      ['中药', '方剂', '理论', '文献'],
    );
  });

  it('同一来源的多个证据聚合成一条来源', () => {
    const groups = buildEvidenceGroups([
      resourceCitation({ chunk_id: 'v1', score: 0.4 }),
      resourceCitation({ chunk_id: 'v2', score: 0.75 }),
    ]);
    assert.equal(groups.length, 1);
    assert.equal(groups[0].source_count, 1);
    assert.equal(groups[0].evidence_count, 2);
    assert.equal(groups[0].evidence_level, 'high'); // 组级别取最高分
    assert.deepEqual(
      groups[0].sources[0].evidences.map((e) => e.score),
      [0.75, 0.4],
    ); // 证据按分数降序
  });

  it('分组按最高分降序', () => {
    const groups = buildEvidenceGroups([
      resourceCitation({ resource_type: 'literature', resource_name: '温病条辨', score: 0.45 }),
      resourceCitation({ resource_type: 'prescription', resource_name: '银翘散', score: 0.88 }),
      docCitation({ score: 0.6 }),
    ]);
    assert.deepEqual(
      groups.map((g) => g.group_key),
      ['resource:prescription', 'document', 'resource:literature'],
    );
  });
});

describe('summarizeEvidence', () => {
  it('统计证据数 / 来源数 / 分组数 / 等级分布', () => {
    const citations = [
      docCitation({ score: 0.9 }),
      docCitation({ chunk_id: 'c2', doc_id: 'd2', doc_name: '本草纲目.pdf', score: 0.4 }),
      resourceCitation({ score: 0.5 }),
    ];
    const groups = buildEvidenceGroups(citations);
    const summary = summarizeEvidence(citations, groups);
    assert.equal(summary.evidence_count, 3);
    assert.equal(summary.source_count, 3);
    assert.equal(summary.group_count, 2);
    assert.equal(summary.max_score, 0.9);
    assert.deepEqual(summary.by_level, { high: 1, medium: 2, insufficient: 0 });
  });
});
