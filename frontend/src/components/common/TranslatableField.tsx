'use client';

import { useEffect, useState } from 'react';
import { Segmented, Spin, Tag, Typography, theme } from 'antd';
import { GlobalOutlined } from '@ant-design/icons';
import { getEvidenceTranslation, needsTranslation } from '@/utils/translation';

const { Text, Paragraph } = Typography;

/**
 * 资源详情字段的「原文 / 中文翻译 / 对照」切换组件（问题1）。
 *
 * 复用证据面板的翻译能力（utils/translation + 后端 /chat/evidence/translate），
 * 规则与证据面板一致：
 * - 默认译文、可切原文/对照、明确标注"AI 翻译"
 * - 中文原文显示"原文已为中文"，少量英文术语不翻译
 * - 不修改数据库中任何原始数据
 */
export function TranslatableField({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  const text = value || '';
  const needTrans = needsTranslation(text);
  const [mode, setMode] = useState<'translation' | 'original' | 'both'>(
    needTrans ? 'translation' : 'original',
  );
  const [translation, setTranslation] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const { token } = theme.useToken();

  useEffect(() => {
    if (!needTrans || translation !== null || loading) return;
    let cancelled = false;
    setLoading(true);
    void getEvidenceTranslation(text).then((res) => {
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
  }, [needTrans, text, translation, loading]);

  const originalBlock = (
    <Paragraph
      style={{ margin: 0, whiteSpace: 'pre-wrap', color: token.colorTextSecondary, fontSize: 13 }}
      ellipsis={mode === 'both' ? { rows: 4, expandable: true, symbol: '展开全文' } : false}
    >
      {text || '—'}
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
          ellipsis={mode === 'both' ? { rows: 4, expandable: true, symbol: '展开全文' } : false}
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
        {text && (
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
            原文已为中文
          </Text>
        )}
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
