'use client';

import type { ReactNode } from 'react';
import { Alert, Space, Tag, Tooltip, Typography } from 'antd';
import { CheckOutlined, ClockCircleOutlined, CloseOutlined, LoadingOutlined } from '@ant-design/icons';
import type { ProcessingStage, ProcessingView } from '@/types';
import {
  formatResultCounts,
  formatStageProgress,
  nextActionLabel,
  processingAlertType,
  processingStatusColor,
  processingStatusLabel,
} from '@/utils/processingStatus';

const { Text } = Typography;

interface TagProps {
  /** 统一状态词汇（pending / processing / awaiting_confirm / success / failed）。 */
  status: string;
  /** 业务自己的细粒度文案，如「切片就绪」「已生成关联记录」；只在悬浮说明里出现。 */
  detail?: string;
  /** 处理概览（来自 processing 接口）；有则阶段与失败原因如实展示。 */
  view?: ProcessingView | null;
  /** 业务侧已知的失败原因（如组内成员的 error_message），原样展示不润色。 */
  error?: string | null;
}

/**
 * 三条链路共用的状态标签：同一个词只有一种颜色和一句解释。
 *
 * 标签本体只显示统一状态；阶段进度、结果计数、失败原因与阻塞原因放在悬浮说明里，
 * 内容全部来自后端派生字段，没有任何估算出来的百分比。
 */
export function ProcessingStatusTag({ status, detail, view, error }: TagProps) {
  const color = processingStatusColor(status);
  const label = processingStatusLabel(status);
  const lines: string[] = [];
  if (view) lines.push(formatStageProgress(view));
  if (detail) lines.push(`业务状态：${detail}`);
  const counts = formatResultCounts(view);
  if (counts) lines.push(`结果：${counts}`);
  if (error) lines.push(`原因：${error}`);
  if (view?.blocked_reason) lines.push(`阻塞：${view.blocked_reason}`);
  if (view?.reason) lines.push(`说明：${view.reason}`);
  const next = nextActionLabel(view?.next_action);
  if (next) lines.push(`下一步：${next}`);

  const tag = <Tag color={color}>{label}</Tag>;
  if (!lines.length) return tag;
  return (
    <Tooltip
      title={
        <div style={{ maxWidth: 360 }}>
          {lines.map((line) => (
            <div key={line}>{line}</div>
          ))}
        </div>
      }
    >
      {tag}
    </Tooltip>
  );
}

const STAGE_ICON: Record<ProcessingStage['state'], string> = {
  done: '✓',
  active: '…',
  pending: '·',
  failed: '✗',
};

/** 阶段底色：与 ProcessingStatusTag 用的 AntD preset 色同一语义（绿/蓝/灰/红）。 */
const STAGE_STYLE: Record<ProcessingStage['state'], { background: string; color: string }> = {
  done: { background: 'rgba(82,196,26,0.12)', color: 'rgba(0,0,0,0.88)' },
  active: { background: 'rgba(24,144,255,0.12)', color: 'rgba(0,0,0,0.88)' },
  pending: { background: 'rgba(0,0,0,0.04)', color: 'rgba(0,0,0,0.45)' },
  failed: { background: 'rgba(255,77,79,0.12)', color: 'rgba(0,0,0,0.88)' },
};

/**
 * 阶段链：每个阶段的真实状态逐条列出（done / active / pending / failed）。
 *
 * 用 Tag 而不是 Steps，是因为关联文件组有 7 步，横向 Steps 在抽屉里会挤成一团；
 * 纵向逐行还能把「卡在哪一步」和原因并排写清楚。
 */
export function ProcessingStages({ view }: { view: ProcessingView | null | undefined }) {
  if (!view) return null;
  const counts = formatResultCounts(view);
  return (
    <div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
        {view.stages.map((stage) => {
          const tone = STAGE_STYLE[stage.state];
          return (
            <span
              key={stage.index}
              style={{
                padding: '0 8px',
                borderRadius: 4,
                fontSize: 12,
                background: tone.background,
                color: tone.color,
              }}
            >
              {stage.state === 'active' ? (
                // 只有后端明确说「处理中」才转圈；等待用户操作 / 已排队时用静态时钟，
                // 否则「等你去点下一步」看起来会像任务卡死。
                view.status === 'processing' ? (
                  <LoadingOutlined style={{ marginRight: 4 }} />
                ) : (
                  <ClockCircleOutlined style={{ marginRight: 4 }} />
                )
              ) : stage.state === 'done' ? (
                <CheckOutlined style={{ marginRight: 4 }} />
              ) : stage.state === 'failed' ? (
                <CloseOutlined style={{ marginRight: 4 }} />
              ) : (
                <span style={{ marginRight: 4 }}>{STAGE_ICON[stage.state]}</span>
              )}
              {stage.label}
            </span>
          );
        })}
      </div>
      {(counts || view.reason || view.blocked_reason) && (
        <div style={{ marginTop: 6 }}>
          <Text type={view.blocked_reason ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
            {[counts && `结果：${counts}`, view.blocked_reason, view.reason]
              .filter(Boolean)
              .join(' ｜ ')}
          </Text>
        </div>
      )}
    </div>
  );
}

interface PanelProps {
  view: ProcessingView | null;
  error?: string | null;
  /** 业务补充信息（重复文件提示、组内成员状态等），由调用方传入。 */
  extra?: ReactNode;
  /** 面板标题，默认「处理概览」。 */
  title?: string;
}

/**
 * 处理概览面板：抽屉顶部一块区域说清「处理过没有 / 走到哪一步 / 为什么停 / 产生了什么」。
 *
 * 三条链路共用同一布局与同一状态词汇；内容全部来自后端 processing 接口，
 * 没有估算百分比，也没有前端自己拼出来的进度。
 */
export function ProcessingOverviewPanel({ view, error, extra, title = '处理概览' }: PanelProps) {
  if (!view && !error) return null;
  const status = view?.status ?? 'pending';
  const next = nextActionLabel(view?.next_action);
  return (
    <Alert
      type={processingAlertType(status)}
      showIcon
      message={
        <Space size={8} wrap>
          <Text strong>{title}</Text>
          {view ? <ProcessingStatusTag status={view.status} view={view} /> : <Tag>读取失败</Tag>}
          {view && <Text type="secondary">{formatStageProgress(view)}</Text>}
          {next && <Text type="secondary">下一步：{next}</Text>}
        </Space>
      }
      description={
        <>
          {view && <ProcessingStages view={view} />}
          {error && (
            <Text type="danger" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
              概览读取失败：{error}
            </Text>
          )}
          {extra && <div style={{ marginTop: 6 }}>{extra}</div>}
        </>
      }
      style={{ marginBottom: 8 }}
    />
  );
}
