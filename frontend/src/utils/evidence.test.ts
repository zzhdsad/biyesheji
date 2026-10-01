/**
 * 阶段十（沿用）+ 阶段十六：前端多源证据逻辑测试。
 *
 * 运行：npm test（node --test，无需额外测试框架；src/utils/evidence.test.ts）
 *
 * 覆盖：证据等级规则、Document / Resource 归一化、四类资源分组、
 *       Document + Resource 混合分组、同来源聚合、分组排序、汇总计数；
 *       阶段十六补充：KG 证据分组口径、source_label 缺失、可选字段为 null、
 *       空证据、弱证据、多来源混合排序。
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  buildEvidenceGroups,
  displayRelevanceScore,
  evidenceLevel,
  normalizeEvidence,
  normalizeEvidenceGroups,
  resolveCitationKey,
  summarizeEvidence,
} from './evidence.ts';
import type { Citation, EvidenceGroup } from '@/types';

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

// ── 相关度展示（BUG-（相关度 3%））───────────────────────────────────────────

describe('displayRelevanceScore', () => {
  it('relevance_score=0.85 → 返回 0.85（用户可见相关度）', () => {
    // 复现「丹参有什么功效」场景：dense 高（0.85）但 reranker sigmoid 低（0.0297）。
    // 修复前 EvidencePanel 会渲染成「相关度 3%」（取 ev.score），误导用户；
    // 修复后必须优先取 relevance_score，渲染成「相关度 85%」。
    assert.equal(
      displayRelevanceScore({ score: 0.0297, relevance_score: 0.85 }),
      0.85,
    );
  });

  it('relevance_score=0.35 → 返回 0.35（中等）', () => {
    assert.equal(
      displayRelevanceScore({ score: 0.5, relevance_score: 0.35 }),
      0.35,
    );
  });

  it('relevance_score=0 → 返回 0', () => {
    assert.equal(
      displayRelevanceScore({ score: 0.1, relevance_score: 0 }),
      0,
    );
  });

  it('relevance_score=null → fallback 到 ev.score', () => {
    // 兼容历史消息 / 旧 fixture / 缺 dense 的命中
    assert.equal(
      displayRelevanceScore({ score: 0.74, relevance_score: null }),
      0.74,
    );
  });

  it('relevance_score=undefined → fallback 到 ev.score', () => {
    assert.equal(
      displayRelevanceScore({ score: 0.6 }),
      0.6,
    );
  });

  it('relevance_score=NaN → fallback 到 ev.score', () => {
    assert.equal(
      displayRelevanceScore({ score: 0.4, relevance_score: NaN }),
      0.4,
    );
  });

  it('relevance_score 越界（>1） → fallback 到 ev.score', () => {
    // 防御：防御性编程，理论上后端不会发送越界值
    assert.equal(
      displayRelevanceScore({ score: 0.5, relevance_score: 1.5 }),
      0.5,
    );
  });

  it('relevance_score 越界（<0） → fallback 到 ev.score', () => {
    assert.equal(
      displayRelevanceScore({ score: 0.5, relevance_score: -0.1 }),
      0.5,
    );
  });

  it('两者都缺失 → 返回 null（EvidencePanel 不渲染相关度 Tag，不出现 NaN%）', () => {
    assert.equal(displayRelevanceScore({}), null);
    assert.equal(displayRelevanceScore({ score: undefined, relevance_score: null }), null);
    assert.equal(displayRelevanceScore({ score: NaN, relevance_score: NaN }), null);
  });

  it('不能错误采用 ev.score 即使 ev.score 比 relevance_score 高', () => {
    // 边界保护：dense=0.2、reranker=0.9 → 必须返回 0.2（按 dense 展示），
    // 不能因 reranker 高就显示 90%。
    assert.equal(
      displayRelevanceScore({ score: 0.9, relevance_score: 0.2 }),
      0.2,
    );
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

  // ── 量纲对齐回归（BUG-：分组 Insufficient / 子证据 Medium 69%）───────────────

  it('relevance_score=0.85、score=0.03 → group/source 均为 high，不再 Insufficient', () => {
    // 复现「丹参有什么功效」：dense cosine 0.85、reranker sigmoid 0.03。
    // 修复前：max(e.score)=0.03 < 0.3 → 分组显示 Insufficient，与子证据 Medium 矛盾。
    const groups = buildEvidenceGroups([
      docCitation({ score: 0.03, relevance_score: 0.85, doc_name: '严氏济生方.txt' }),
    ]);
    const g = groups[0];
    assert.equal(g.max_score, 0.85);
    assert.equal(g.evidence_level, 'high');
    assert.equal(g.sources[0].max_score, 0.85);
    assert.equal(g.sources[0].evidence_level, 'high');
    // 子证据自身 level 与相关度展示不变
    assert.equal(g.sources[0].evidences[0].evidence_level, 'high');
    assert.equal(displayRelevanceScore(g.sources[0].evidences[0]), 0.85);
  });

  it('relevance_score 缺失 → fallback 到 ev.score（历史消息 / 旧 fixture 兼容）', () => {
    const groups = buildEvidenceGroups([docCitation({ score: 0.74 })]);
    assert.equal(groups[0].max_score, 0.74);
    assert.equal(groups[0].evidence_level, 'high');
  });

  it('relevance_score 低（0.2）时不能被 reranker 高分（0.9）推高等级', () => {
    const groups = buildEvidenceGroups([docCitation({ score: 0.9, relevance_score: 0.2 })]);
    assert.equal(groups[0].max_score, 0.2);
    assert.equal(groups[0].evidence_level, 'insufficient');
  });

  it('多证据：group.max_score 取 relevance_score 最大值，排序仍按 ev.score', () => {
    const groups = buildEvidenceGroups([
      docCitation({ chunk_id: 'v1', score: 0.03, relevance_score: 0.69, doc_name: 'A.txt' }),
      docCitation({ chunk_id: 'v2', score: 0.05, relevance_score: 0.9, doc_name: 'A.txt' }),
    ]);
    const g = groups[0];
    assert.equal(g.max_score, 0.9); // dense 最大值
    assert.equal(g.evidence_level, 'high');
    // 证据排序按 ev.score（reranker 位次）降序，语义不变
    assert.deepEqual(
      g.sources[0].evidences.map((e) => e.score),
      [0.05, 0.03],
    );
  });
});

/**
 * 阶段十六：KG 证据（阶段十三引入，source_kind='kg'）必须与同名 Resource
 * 分组区分开，且与后端 application/evidence.py group_evidence 口径一致。
 */
/** overrides 允许传 null（后端可能把可选字段序列化为 null）。 */
function kgCitation(overrides: Record<string, unknown> = {}): Citation {
  return {
    chunk_id: 'kg:e1',
    source_index: 3,
    doc_id: 'kg:e1',
    doc_name: '金银花',
    content: '银翘散 → contains → 金银花',
    score: 0.9,
    source_kind: 'kg',
    resource_type: 'herb',
    resource_id: 'herb-1',
    resource_name: '金银花',
    source_label: '图谱·中药',
    source_name: '金银花',
    source_id: 'herb-1',
    title_path: '银翘散 → contains → 金银花',
    page_num: null,
    ...overrides,
  } as Citation;
}

describe('阶段十六：KG 证据归一化与分组', () => {
  it('KG 证据保持 kg 类别，不降级为 resource', () => {
    const ev = normalizeEvidence(kgCitation());
    assert.equal(ev.source_kind, 'kg');
    assert.equal(ev.resource_type, 'herb');
    assert.equal(ev.source_name, '金银花');
    // 后端已下发 source_label（图谱·中药）时原样保留
    assert.equal(ev.source_label, '图谱·中药');
  });

  it('KG 证据缺后端标签时本地推导为「知识图谱·中药」', () => {
    const ev = normalizeEvidence(kgCitation({ source_label: undefined }));
    assert.equal(ev.source_label, '知识图谱·中药');
  });

  it('KG 与同名 Resource 分成不同组（kg:herb vs resource:herb）', () => {
    const groups = buildEvidenceGroups([kgCitation(), resourceCitation()]);
    assert.deepEqual(
      groups.map((g) => g.group_key),
      ['kg:herb', 'resource:herb'],
    );
    assert.deepEqual(
      groups.map((g) => g.source_label),
      ['知识图谱·中药', '中药'],
    );
    assert.equal(groups[0].source_kind, 'kg');
    assert.equal(groups[1].source_kind, 'resource');
  });

  it('只有 KG 证据 → 单组且计数正确', () => {
    const groups = buildEvidenceGroups([
      kgCitation(),
      kgCitation({ chunk_id: 'kg:e2', source_index: 4, score: 0.8 }),
    ]);
    assert.equal(groups.length, 1);
    assert.equal(groups[0].source_count, 1);
    assert.equal(groups[0].evidence_count, 2);
    assert.equal(groups[0].evidence_level, 'high');
  });

  it('KG 证据缺 resource_type → 归入 kg:kg 兜底组', () => {
    const groups = buildEvidenceGroups([
      kgCitation({ resource_type: null, source_label: undefined }),
    ]);
    assert.equal(groups[0].group_key, 'kg:kg');
    assert.equal(groups[0].source_label, '知识图谱·kg');
  });

  it('Vector Resource + KG + Document 三组互不合并', () => {
    const citations = [
      docCitation({ score: 0.75 }),
      resourceCitation({ score: 0.85 }),
      kgCitation({ score: 0.95 }),
    ];
    const groups = buildEvidenceGroups(citations);
    assert.deepEqual(
      groups.map((g) => g.group_key).sort(),
      ['document', 'kg:herb', 'resource:herb'],
    );
    const summary = summarizeEvidence(citations, groups);
    assert.equal(summary.evidence_count, 3);
    assert.equal(summary.group_count, 3);
    assert.deepEqual(summary.by_level, { high: 3, medium: 0, insufficient: 0 });
  });

  it('KG 证据按分数参与组间排序', () => {
    const groups = buildEvidenceGroups([
      docCitation({ score: 0.8 }),
      kgCitation({ score: 0.95 }),
      resourceCitation({ score: 0.72 }),
    ]);
    assert.deepEqual(
      groups.map((g) => g.group_key),
      ['kg:herb', 'document', 'resource:herb'],
    );
  });
});

describe('阶段十六：缺省 / 空值 / 弱证据健壮性', () => {
  it('空证据：分组与汇总均为零值且不抛错', () => {
    const groups = buildEvidenceGroups([]);
    assert.deepEqual(groups, []);
    const summary = summarizeEvidence([]);
    assert.equal(summary.evidence_count, 0);
    assert.equal(summary.source_count, 0);
    assert.equal(summary.group_count, 0);
    assert.equal(summary.max_score, 0);
    assert.deepEqual(summary.by_level, { high: 0, medium: 0, insufficient: 0 });
  });

  it('弱证据（低分）仍分组，等级为 insufficient', () => {
    const groups = buildEvidenceGroups([
      docCitation({ score: 0.1 }),
      resourceCitation({ score: 0.05, chunk_id: 'v9' }),
    ]);
    assert.equal(groups.length, 2);
    assert.ok(['insufficient', 'medium'].includes(groups[0].evidence_level));
    const summary = summarizeEvidence([
      docCitation({ score: 0.1 }),
      resourceCitation({ score: 0.05, chunk_id: 'v9' }),
    ]);
    assert.deepEqual(summary.by_level, { high: 0, medium: 0, insufficient: 2 });
  });

  it('source_name / source_label / source_id 缺失时补齐默认值', () => {
    const broken: Citation = {
      chunk_id: 'c-x',
      source_index: 1,
      doc_id: '',
      doc_name: '',
      content: '',
      score: undefined,
      source_kind: 'resource',
      resource_type: 'literature',
      resource_id: null,
      resource_name: null,
    };
    const ev = normalizeEvidence(broken);
    assert.equal(ev.source_name, '资源');
    assert.equal(ev.source_label, '文献');
    assert.equal(ev.score, 0);
    assert.equal(ev.evidence_level, 'insufficient');
    const groups = buildEvidenceGroups([broken]);
    assert.equal(groups[0].sources[0].source_name, '资源');
  });

  it('可选字段全为 null 时不做空指针访问', () => {
    // 后端可能把可选字段序列化为 null（前端类型声明为可选），此处显式验证容忍性
    const broken = kgCitation({
      page_num: null,
      title_path: null,
      source_type: null,
      era: null,
      credibility_level: null,
      source_label: null,
    }) as unknown as Citation;
    const ev = normalizeEvidence(broken);
    assert.equal(ev.page_num, null);
    assert.equal(ev.title_path, null);
    assert.equal(ev.source_label, '知识图谱·中药');
    assert.equal(ev.evidence_level, 'high');
  });

  it('历史消息（阶段十之前）仍按 Document 处理，不被 KG 分支影响', () => {
    const legacy: Citation = {
      chunk_id: 'old-1',
      source_index: 2,
      doc_id: 'd-old',
      doc_name: '旧.pdf',
      content: '旧证据',
      score: 0.66,
      resource_type: null,
    };
    const groups = buildEvidenceGroups([legacy]);
    assert.equal(groups[0].group_key, 'document');
    assert.equal(groups[0].sources[0].source_label, '文档');
  });
});

// ── 后端 evidence_groups 直用（BUG-：分组 Insufficient / 子证据 Medium 69%）──

describe('normalizeEvidenceGroups（后端 evidence_groups 优先）', () => {
  /** 模拟后端 group_evidence 的真实输出（丹参场景：dense 0.6946 / rerank 0.031）。 */
  function backendGroups(): EvidenceGroup[] {
    return [
      {
        group_key: 'document',
        source_kind: 'document',
        source_type: null,
        source_label: '文档',
        source_count: 1,
        evidence_count: 1,
        max_score: 0.6946,
        evidence_level: 'medium',
        sources: [
          {
            source_id: 'doc-1',
            source_name: '严氏济生方.txt',
            source_kind: 'document',
            source_type: null,
            source_label: '文档',
            evidence_count: 1,
            max_score: 0.6946,
            evidence_level: 'medium',
            evidences: [
              {
                chunk_id: 'chunk-1',
                source_index: 1,
                doc_id: 'doc-1',
                doc_name: '严氏济生方.txt',
                content: '丹参，主心腹邪气',
                score: 0.031,
                relevance_score: 0.6946,
                evidence_level: 'medium',
              } as Citation,
            ],
          },
        ],
      } as EvidenceGroup,
    ];
  }

  it('直接沿用后端 max_score / evidence_level（不重复计算，不显示 Insufficient）', () => {
    const groups = normalizeEvidenceGroups(backendGroups());
    assert.equal(groups[0].max_score, 0.6946);
    assert.equal(groups[0].evidence_level, 'medium');
    assert.equal(groups[0].sources[0].max_score, 0.6946);
    assert.equal(groups[0].sources[0].evidence_level, 'medium');
    // 后端算好的等级绝不能被前端用 rerank score 重算成 insufficient
    assert.notEqual(groups[0].evidence_level, 'insufficient');
  });

  it('evidences 补齐展示字段（source_label / evidence_text）', () => {
    const groups = normalizeEvidenceGroups(backendGroups());
    const ev = groups[0].sources[0].evidences[0];
    assert.equal(ev.source_label, '文档');
    assert.equal(ev.evidence_text, '丹参，主心腹邪气');
    // reranker score 原样保留（相关度展示用 relevance_score）
    assert.equal(ev.score, 0.031);
    assert.equal(displayRelevanceScore(ev), 0.6946);
  });

  it('后端组缺失 max_score / evidence_level（防御）→ 按 groupScore 兜底重算', () => {
    const raw = backendGroups() as unknown as {
      sources: { max_score?: number; evidence_level?: string; evidences: unknown[] }[];
    }[];
    delete raw[0].sources[0].max_score;
    delete raw[0].sources[0].evidence_level;
    const groups = normalizeEvidenceGroups(raw as unknown as EvidenceGroup[]);
    assert.equal(groups[0].sources[0].max_score, 0.6946); // dense 优先，非 0.031
    assert.equal(groups[0].sources[0].evidence_level, 'medium');
  });
});

describe('summarizeEvidence 量纲（BUG-：max_score 用 rerank 导致与 by_level 矛盾）', () => {
  it('max_score 优先 relevance_score，与 by_level 同量纲', () => {
    const citations = [docCitation({ score: 0.03, relevance_score: 0.85 })];
    const summary = summarizeEvidence(citations);
    assert.equal(summary.max_score, 0.85); // 不是 0.03
    assert.deepEqual(summary.by_level, { high: 1, medium: 0, insufficient: 0 });
  });

  it('relevance_score 缺失 → fallback 到 ev.score', () => {
    const summary = summarizeEvidence([docCitation({ score: 0.74 })]);
    assert.equal(summary.max_score, 0.74);
  });

  it('多证据取 relevance_score 最大值', () => {
    const summary = summarizeEvidence([
      docCitation({ chunk_id: 'v1', score: 0.03, relevance_score: 0.4 }),
      docCitation({ chunk_id: 'v2', score: 0.05, relevance_score: 0.9 }),
    ]);
    assert.equal(summary.max_score, 0.9);
    assert.deepEqual(summary.by_level, { high: 1, medium: 1, insufficient: 0 });
  });
});

// ── BUG-058：内联引用编号 → 证据面板 key 映射 ────────────────────────────────

describe('resolveCitationKey（BUG-058）', () => {
  it('编号命中现有 source_index → 返回该 key', () => {
    assert.equal(resolveCitationKey(1, [1, 2, 3]), '1');
    assert.equal(resolveCitationKey(3, [1, 2, 3]), '3');
  });

  it('编号越界（模型引用了不存在的来源）→ 返回 null，调用方忽略点击', () => {
    assert.equal(resolveCitationKey(5, [1, 2, 3]), null);
    assert.equal(resolveCitationKey(0, [1, 2, 3]), null);
  });

  it('无来源 / 编号非法 → 一律 null（不臆造来源）', () => {
    assert.equal(resolveCitationKey(1, []), null);
    assert.equal(resolveCitationKey(Number.NaN, [1]), null);
  });

  it('编号不连续时按实际集合精确匹配（不按下标猜测）', () => {
    const indexes = [2, 4, 7];
    assert.equal(resolveCitationKey(4, indexes), '4');
    assert.equal(resolveCitationKey(3, indexes), null);
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
