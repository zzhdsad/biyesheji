/**
 * 阶段十：多源证据（Evidence）前端逻辑。
 *
 * 与后端 src/application/evidence.py 语义保持一致：
 * - 证据等级沿用既有规则：score >= 0.7 → high，>= 0.3 → medium，否则 insufficient；
 * - Document 与 Resource（herb / prescription / theory / literature）统一为同一
 *   Evidence 结构，不为四种资源各建一套结构；
 * - 分组顺序：先按 source_kind（+ 细分类型），组内再按具体来源聚合，均按最高分降序。
 *
 * 该模块只依赖类型（import type），无运行时依赖，便于独立测试。
 */
import type {
  Citation,
  EvidenceGroup,
  EvidenceLevel,
  EvidenceSource,
  EvidenceSummary,
} from '@/types';

/** 证据等级阈值（与后端一致，不在前端自行改动）。 */
export const EVIDENCE_HIGH_THRESHOLD = 0.7;
export const EVIDENCE_MEDIUM_THRESHOLD = 0.3;

/** 来源类别 → 中文标签（阶段十三：kg 与后端 SOURCE_KIND_LABELS 对齐）。 */
export const SOURCE_KIND_LABEL: Record<string, string> = {
  document: '文档',
  resource: '资源',
  kg: '知识图谱',
};

/** Resource 细分类型 → 中文标签（与后端 RESOURCE_TYPE_LABELS 一致）。 */
export const RESOURCE_TYPE_LABEL: Record<string, string> = {
  herb: '中药',
  prescription: '方剂',
  theory: '理论',
  literature: '文献',
};

/** 证据等级 → 展示文案。 */
export const EVIDENCE_LEVEL_LABEL: Record<EvidenceLevel, string> = {
  high: 'High',
  medium: 'Medium',
  insufficient: 'Insufficient',
};

/** 证据等级 → Tag 颜色。 */
export const EVIDENCE_LEVEL_COLOR: Record<EvidenceLevel, string> = {
  high: 'success',
  medium: 'warning',
  insufficient: 'default',
};

/** 按分数推导证据等级（与后端 evidence_level 一致）。 */
export function evidenceLevel(score: number): EvidenceLevel {
  if (score >= EVIDENCE_HIGH_THRESHOLD) return 'high';
  if (score >= EVIDENCE_MEDIUM_THRESHOLD) return 'medium';
  return 'insufficient';
}

/**
 * 归一化为统一 Evidence：补齐 source_kind / source_id / source_name /
 * source_label / evidence_text / evidence_level。
 *
 * 历史消息（阶段十之前持久化）缺少这些字段，归一化后可与新数据同样分组展示。
 */
export function normalizeEvidence(citation: Citation): Citation {
  const score = citation.score ?? 0;
  // 阶段十三：KG 证据后端同时带 source_kind='kg' 与 resource_type，必须与 resource
  // 区分开（后端 group_evidence 把 KG 单独成组 kg:<type>，标签为「知识图谱·中药」）。
  const isKg = citation.source_kind === 'kg';
  const isResource =
    !isKg && (citation.source_kind === 'resource' || Boolean(citation.resource_type));
  const resourceType = isKg || isResource ? citation.resource_type ?? null : null;

  const sourceId = isKg || isResource
    ? citation.resource_id ?? citation.source_id ?? citation.doc_id
    : citation.source_id ?? citation.doc_id;
  // 阶段十六：名称取值用 ||（而非 ??），空字符串按缺失处理，
  // 避免出现「来源名称为空」的空白证据卡片。
  const sourceName = isKg || isResource
    ? citation.resource_name || citation.source_name || citation.doc_name || '资源'
    : citation.source_name || citation.doc_name || '未知文档';
  // 优先沿用后端下发的 source_label（后端对 KG 已标注「图谱·中药」），缺失时本地推导
  const fallbackLabel = isKg
    ? `${SOURCE_KIND_LABEL.kg}·${resourceType ? RESOURCE_TYPE_LABEL[resourceType] ?? resourceType : SOURCE_KIND_LABEL.kg}`
    : isResource
      ? (resourceType ? RESOURCE_TYPE_LABEL[resourceType] ?? resourceType : SOURCE_KIND_LABEL.resource)
      : SOURCE_KIND_LABEL.document;

  return {
    ...citation,
    score,
    source_kind: isKg ? 'kg' : isResource ? 'resource' : 'document',
    resource_type: resourceType,
    evidence_level: citation.evidence_level ?? evidenceLevel(score),
    evidence_id: citation.evidence_id ?? citation.chunk_id,
    source_id: sourceId,
    source_name: sourceName,
    source_label: citation.source_label ?? fallbackLabel,
    evidence_text: citation.evidence_text ?? citation.content,
  };
}

function groupKeyOf(evidence: Citation): { groupKey: string; label: string; subtype: string | null } {
  // 阶段十三：KG 证据独立成组 kg:<type>（与后端 group_evidence 口径一致），
  // 避免 Knowledge Graph 关系证据被混进同名 Resource 组。
  if (evidence.source_kind === 'kg') {
    const subtype = evidence.resource_type ?? 'kg';
    return {
      groupKey: `kg:${subtype}`,
      label: `${SOURCE_KIND_LABEL.kg}·${RESOURCE_TYPE_LABEL[subtype] ?? subtype}`,
      subtype,
    };
  }
  if (evidence.source_kind === 'resource') {
    const subtype = evidence.resource_type ?? 'resource';
    return {
      groupKey: `resource:${subtype}`,
      label: RESOURCE_TYPE_LABEL[subtype] ?? subtype,
      subtype,
    };
  }
  return { groupKey: 'document', label: SOURCE_KIND_LABEL.document, subtype: null };
}

/**
 * 多来源证据分组：先按来源类别（+ 细分类型），组内再按具体来源聚合。
 *
 * 例：中药组下可同时有「金银花」「连翘」两个来源，每个来源下有多条证据。
 */
export function buildEvidenceGroups(citations: Citation[]): EvidenceGroup[] {
  const evidence = citations.map(normalizeEvidence);
  const groupMap = new Map<string, EvidenceGroup>();
  const orderedGroups: EvidenceGroup[] = [];
  const sourceIndex = new Map<string, EvidenceSource[]>();

  for (const ev of evidence) {
    const { groupKey, label, subtype } = groupKeyOf(ev);
    let group = groupMap.get(groupKey);
    if (!group) {
      group = {
        group_key: groupKey,
        source_kind: ev.source_kind ?? 'document',
        source_type: subtype,
        source_label: label,
        source_count: 0,
        evidence_count: 0,
        max_score: 0,
        evidence_level: 'insufficient',
        sources: [],
      };
      groupMap.set(groupKey, group);
      orderedGroups.push(group);
      sourceIndex.set(groupKey, []);
    }
    const sources = sourceIndex.get(groupKey) as EvidenceSource[];
    const sid = ev.source_id ?? ev.source_name ?? groupKey;
    let source = sources.find((s) => (s.source_id ?? s.source_name) === sid);
    if (!source) {
      source = {
        source_id: ev.source_id ?? null,
        // 阶段十六：空字符串同样按缺失处理，避免展示空名来源
        source_name: ev.source_name || '未知来源',
        source_kind: ev.source_kind ?? 'document',
        source_type: subtype,
        source_label: label,
        evidence_count: 0,
        max_score: 0,
        evidence_level: 'insufficient',
        evidences: [],
      };
      sources.push(source);
      group.sources.push(source);
    }
    source.evidences.push(ev);
  }

  for (const group of orderedGroups) {
    for (const source of group.sources) {
      source.evidences.sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
      source.evidence_count = source.evidences.length;
      source.max_score = source.evidences.reduce((m, e) => Math.max(m, e.score ?? 0), 0);
      source.evidence_level = evidenceLevel(source.max_score);
    }
    group.sources.sort((a, b) => b.max_score - a.max_score);
    group.source_count = group.sources.length;
    group.evidence_count = group.sources.reduce((n, s) => n + s.evidence_count, 0);
    group.max_score = group.sources.reduce((m, s) => Math.max(m, s.max_score), 0);
    group.evidence_level = evidenceLevel(group.max_score);
  }

  // 组间按最高分降序（同分保持首次出现顺序）
  orderedGroups.sort((a, b) => b.max_score - a.max_score);
  return orderedGroups;
}

/**
 * 把答案内联引用标记 `[citation: n]` 的编号 n 映射到 Collapse 面板 key（BUG-058）。
 *
 * 面板 key 是证据的 `source_index`（后端下发，即展示位次），而内联编号由 LLM
 * 生成——模型可能引用越界编号（只有 3 条来源却写 `[citation: 5]`）。旧实现直接
 * 把 n 当 key：越界编号点不出任何面板（且再点也不会收起已展开项）。
 *
 * 映射规则：
 * - 命中现有 `source_index` → 返回 key = String(n)；
 * - 越界（无对应来源）→ 返回 null，调用方应忽略本次点击（不展开、不报错）；
 * - 编号重复/缺失时按现有集合精确匹配，绝不臆造来源。
 */
export function resolveCitationKey(
  chipIndex: number,
  availableSourceIndexes: number[],
): string | null {
  if (!Number.isFinite(chipIndex)) return null;
  const wanted = Math.trunc(chipIndex);
  return availableSourceIndexes.includes(wanted) ? String(wanted) : null;
}

/** 证据汇总：证据数 / 来源数 / 分组数 / 各等级数量。 */
export function summarizeEvidence(
  citations: Citation[],
  groups: EvidenceGroup[] = buildEvidenceGroups(citations),
): EvidenceSummary {
  const evidence = citations.map(normalizeEvidence);
  const byLevel: Record<EvidenceLevel, number> = { high: 0, medium: 0, insufficient: 0 };
  for (const ev of evidence) {
    byLevel[ev.evidence_level ?? evidenceLevel(ev.score ?? 0)] += 1;
  }
  return {
    evidence_count: evidence.length,
    source_count: groups.reduce((n, g) => n + g.source_count, 0),
    group_count: groups.length,
    max_score: evidence.reduce((m, e) => Math.max(m, e.score ?? 0), 0),
    by_level: byLevel,
  };
}
