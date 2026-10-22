'use client';

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Collapse, Segmented, Spin, Tag, Tooltip, Typography, theme } from 'antd';
import { FileTextOutlined, GlobalOutlined } from '@ant-design/icons';
import type { Citation, Evidence, EvidenceGroup, EvidenceLevel } from '@/types';
import { credibilityColor } from '@/constants/source';
import {
  EVIDENCE_LEVEL_COLOR,
  EVIDENCE_LEVEL_DESCRIPTION,
  EVIDENCE_LEVEL_LABEL,
  buildEvidenceGroups,
  displayRelevanceScore,
  formatSourceName,
  normalizeEvidenceGroups,
  summarizeEvidence,
} from '@/utils/evidence';
import { getEvidenceTranslation, needsTranslation } from '@/utils/translation';

const { Text, Paragraph } = Typography;

/** 等级 Tag 的统一 Tooltip：让普通用户理解 High/Medium/Insufficient 的含义。 */
function LevelTag({ level }: { level: EvidenceLevel }) {
  return (
    <Tooltip title={EVIDENCE_LEVEL_DESCRIPTION[level]}>
      <Tag color={EVIDENCE_LEVEL_COLOR[level]} style={{ margin: 0, fontSize: 12, cursor: 'help' }}>
        Evidence: {EVIDENCE_LEVEL_LABEL[level]}
      </Tag>
    </Tooltip>
  );
}

/**
 * 证据原文展示区（问题4/5：原文 / AI 中文翻译）。
 *
 * - 原文以中文为主：直接显示原文并标注"原文已为中文"，不提供翻译（避免把
 *   少量英文术语的中文原文"翻译成奇怪的中文"）；
 * - 原文含大量英文：默认显示 AI 中文翻译，可切换「仅原文 / 仅中文 / 对照」；
 * - 翻译懒加载 + 模块级缓存（utils/translation），失败显示统一 fallback；
 * - 译文始终标注「AI 翻译」，绝不冒充原始文献内容；原文保持数据库真实文本。
 */
function EvidenceContent({
  evidenceText,
  token,
}: {
  evidenceText: string;
  token: ReturnType<typeof theme.useToken>['token'];
}) {
  const needTrans = needsTranslation(evidenceText);
  const [mode, setMode] = useState<'translation' | 'original' | 'both'>(
    needTrans ? 'translation' : 'original',
  );
  const [translation, setTranslation] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!needTrans || translation !== null || loading) return;
    let cancelled = false;
    setLoading(true);
    void getEvidenceTranslation(evidenceText).then((res) => {
      if (cancelled) return;
      setLoading(false);
      if (res.error) {
        setError(res.error);
      } else if (res.translation) {
        setTranslation(res.translation);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [needTrans, evidenceText, translation, loading]);

  const originalBlock = (
    <Paragraph
      style={{ margin: 0, whiteSpace: 'pre-wrap', color: token.colorTextSecondary, fontSize: 13 }}
      ellipsis={mode === 'both' ? { rows: 6, expandable: true, symbol: '展开全文' } : false}
    >
      {evidenceText}
    </Paragraph>
  );

  const translationBlock = (
    <div>
      <Tag color="purple" style={{ margin: '0 0 6px 0', fontSize: 12 }} icon={<GlobalOutlined />}>
        AI 翻译
      </Tag>
      {loading ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
          <Spin size="small" /> 正在生成中文翻译…
        </div>
      ) : error ? (
        <Text type="warning" style={{ fontSize: 13 }}>
          {error}
        </Text>
      ) : (
        <Paragraph
          style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 13 }}
          ellipsis={mode === 'both' ? { rows: 6, expandable: true, symbol: '展开全文' } : false}
        >
          {translation}
        </Paragraph>
      )}
    </div>
  );

  if (!needTrans) {
    return (
      <div>
        {originalBlock}
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
          原文已为中文
        </Text>
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        <Segmented
          size="small"
          value={mode}
          onChange={(v) => setMode(v as typeof mode)}
          options={[
            { label: '中文翻译', value: 'translation' },
            { label: '原文', value: 'original' },
            { label: '对照', value: 'both' },
          ]}
        />
      </div>
      {mode !== 'translation' && originalBlock}
      {mode === 'both' && (
        <div style={{ borderTop: `1px dashed ${token.colorBorderSecondary}`, margin: '8px 0' }} />
      )}
      {mode !== 'original' && translationBlock}
    </div>
  );
}

/** 证据卡片头部：来源编号 · 来源名称（书名号化）· 分类 · Level · 相关度。 */
function cardLabel(ev: Evidence, token: ReturnType<typeof theme.useToken>['token']): ReactNode {
  const level = ev.evidence_level ?? 'insufficient';
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
      <Tag color="blue" style={{ margin: 0, fontWeight: 500 }}>
        [{ev.source_index}]
      </Tag>
      <FileTextOutlined style={{ color: token.colorPrimary }} />
      <Text strong style={{ marginRight: 4 }}>
        {/* 问题6：文件名产品化（"普济本事方.txt" → 《普济本事方》） */}
        {formatSourceName(ev.source_name || ev.doc_name)}
      </Text>
      <Tag style={{ margin: 0, fontSize: 12 }}>{ev.source_label}</Tag>
      <LevelTag level={level} />
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
      {(() => {
        // BUG-（相关度 3%）：优先展示 BGE-M3 dense cosine（relevance_score），
        // 缺失时 fallback 到 ev.score（reranker sigmoid）。两者都缺失则不渲染。
        const rel = displayRelevanceScore(ev);
        if (rel == null) return null;
        const pct = Math.round(Math.max(0, Math.min(1, rel)) * 100);
        return (
          <Tag
            style={{
              margin: 0,
              fontSize: 12,
              color: token.colorTextTertiary,
              background: token.colorFillQuaternary,
              border: 'none',
            }}
          >
            相关度 {pct}%
          </Tag>
        );
      })()}
    </div>
  );
}

/**
 * 多来源证据展示面板（阶段十）。
 *
 * 按来源分组展示：文档 / 中药 / 方剂 / 理论 / 文献，组内按具体来源聚合，
 * 每条证据展示来源名称、证据内容（原文 / AI 中文翻译）、Evidence Level
 * 与 Citation 编号，不同来源之间有明确区分（不再把所有 Citation 混成一组）。
 */
export function EvidencePanel({
  citations,
  groups: backendGroups,
  activeKey,
  onChange,
}: {
  citations: Citation[];
  /** 后端下发的 evidence_groups（/ask-stream citations 事件）。 */
  groups?: EvidenceGroup[];
  activeKey: string[];
  onChange: (keys: string[]) => void;
}) {
  // 优先使用后端下发的 evidence_groups（group_evidence 已按 relevance_score
  // 量纲算好 max_score / evidence_level，与 per-citation 一致），避免前端重复
  // 业务计算引入量纲漂移；历史消息 / 旧数据缺失时回退本地 buildEvidenceGroups
  // （内部同样按 relevance_score 优先，见 groupScore）。
  const groups = useMemo(
    () =>
      backendGroups && backendGroups.length > 0
        ? normalizeEvidenceGroups(backendGroups)
        : buildEvidenceGroups(citations),
    [backendGroups, citations],
  );
  const summary = useMemo(() => summarizeEvidence(citations, groups), [citations, groups]);
  // 模块 E：视觉 token 化（修复暗色模式下硬编码浅色失效的问题）
  const { token } = theme.useToken();

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
            <LevelTag level={group.evidence_level} />
          </div>
          <Collapse
            activeKey={activeKey}
            onChange={(keys) => onChange(keys as string[])}
            size="small"
            style={{
              marginTop: 4,
              background: token.colorFillQuaternary,
              borderRadius: 8,
            }}
            items={group.sources.flatMap((source) =>
              source.evidences.map((ev) => ({
                key: String(ev.source_index),
                label: cardLabel(ev, token),
                children: (
                  <div>
                    <EvidenceContent
                      evidenceText={ev.evidence_text || ev.content || ''}
                      token={token}
                    />
                    {/* 来源信息（问题6）：仅展示数据库中真实存在的字段，不凭空生成 */}
                    {ev.source_kind === 'document' &&
                      ev.doc_name &&
                      formatSourceName(ev.doc_name) !== ev.doc_name && (
                        <Text
                          type="secondary"
                          style={{ fontSize: 12, display: 'block', marginTop: 8 }}
                        >
                          原始文件：{ev.doc_name}
                        </Text>
                      )}
                  </div>
                ),
              })),
            )}
          />
        </div>
      ))}
    </div>
  );
}
