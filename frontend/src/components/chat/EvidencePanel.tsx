'use client';

import { useMemo, type ReactNode } from 'react';
import { Collapse, Tag, Typography } from 'antd';
import { FileTextOutlined } from '@ant-design/icons';
import type { Citation, Evidence } from '@/types';
import { credibilityColor } from '@/constants/source';
import {
  EVIDENCE_LEVEL_COLOR,
  EVIDENCE_LEVEL_LABEL,
  buildEvidenceGroups,
  summarizeEvidence,
} from '@/utils/evidence';

const { Text, Paragraph } = Typography;

/** 证据卡片头部：来源编号 · 来源名称 · 来源分类 · Evidence Level · 相关度。 */
function cardLabel(ev: Evidence): ReactNode {
  const level = ev.evidence_level ?? 'insufficient';
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
      <Tag color="blue" style={{ margin: 0, fontWeight: 500 }}>
        [{ev.source_index}]
      </Tag>
      <FileTextOutlined style={{ color: '#1677ff' }} />
      <Text strong style={{ marginRight: 4 }}>
        {ev.source_name || ev.doc_name}
      </Text>
      <Tag style={{ margin: 0, fontSize: 12 }}>{ev.source_label}</Tag>
      <Tag color={EVIDENCE_LEVEL_COLOR[level]} style={{ margin: 0, fontSize: 12 }}>
        Evidence: {EVIDENCE_LEVEL_LABEL[level]}
      </Tag>
      {ev.page_num != null && ev.page_num > 0 && (
        <Text type="secondary">第 {ev.page_num} 页</Text>
      )}
      {ev.source_type && (
        <Tag
          color={credibilityColor(ev.credibility_level)}
          style={{ margin: 0, fontSize: 12 }}
        >
          {ev.source_type}
          {ev.era ? `·${ev.era}` : ''}
          {ev.credibility_level != null ? `·Lv${ev.credibility_level}` : ''}
        </Tag>
      )}
      {ev.title_path && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          · {ev.title_path}
        </Text>
      )}
      {ev.score != null && (
        <Tag
          style={{
            margin: 0,
            fontSize: 12,
            color: '#8c8c8c',
            background: '#fafafa',
            border: 'none',
          }}
        >
          相关度 {(ev.score * 100).toFixed(0)}%
        </Tag>
      )}
    </div>
  );
}

/**
 * 多来源证据展示面板（阶段十）。
 *
 * 按来源分组展示：文档 / 中药 / 方剂 / 理论 / 文献，组内按具体来源聚合，
 * 每条证据展示来源名称、证据内容、Evidence Level 与 Citation 编号，
 * 不同来源之间有明确区分（不再把所有 Citation 混成一组）。
 */
export function EvidencePanel({
  citations,
  activeKey,
  onChange,
}: {
  citations: Citation[];
  activeKey: string[];
  onChange: (keys: string[]) => void;
}) {
  const groups = useMemo(() => buildEvidenceGroups(citations), [citations]);
  const summary = useMemo(() => summarizeEvidence(citations, groups), [citations, groups]);

  if (citations.length === 0) return null;

  return (
    <div style={{ marginTop: 8 }}>
      <Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>
        引用来源 · {summary.source_count} 个来源 / {summary.evidence_count} 条证据
        （点击展开原文）
      </Text>
      {groups.map((group) => (
        <div key={group.group_key} style={{ marginTop: 6 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginLeft: 4 }}>
            <Tag
              // 阶段十三：KG 证据单独着色，与 document / resource 三类来源区分
              color={
                group.source_kind === 'kg'
                  ? 'purple'
                  : group.source_kind === 'resource'
                    ? 'geekblue'
                    : 'blue'
              }
              style={{ margin: 0, fontWeight: 500 }}
            >
              {group.source_label}
            </Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {group.source_count} 个来源 / {group.evidence_count} 条证据
            </Text>
            <Tag
              color={EVIDENCE_LEVEL_COLOR[group.evidence_level]}
              style={{ margin: 0, fontSize: 12 }}
            >
              Evidence: {EVIDENCE_LEVEL_LABEL[group.evidence_level]}
            </Tag>
          </div>
          <Collapse
            activeKey={activeKey}
            onChange={(keys) => onChange(keys as string[])}
            size="small"
            style={{ marginTop: 4, background: '#fafafa', borderRadius: 8 }}
            items={group.sources.flatMap((source) =>
              source.evidences.map((ev) => ({
                key: String(ev.source_index),
                label: cardLabel(ev),
                children: (
                  <Paragraph
                    style={{
                      margin: 0,
                      whiteSpace: 'pre-wrap',
                      color: 'rgba(0,0,0,0.75)',
                      fontSize: 13,
                    }}
                    ellipsis={{ rows: 6, expandable: true, symbol: '展开全文' }}
                  >
                    {ev.evidence_text || ev.content}
                  </Paragraph>
                ),
              })),
            )}
          />
        </div>
      ))}
    </div>
  );
}
