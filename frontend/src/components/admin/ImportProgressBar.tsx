'use client';

import { Progress, Space, Tag, Typography } from 'antd';

import type { ImportProgressSnapshot } from '@/services/api';

const PHASE_LABEL: Record<string, string> = {
  idle: '准备中',
  files: '读取文件',
  parse: '解析文件',
  records: '写入原始记录',
  write: '写入记录与实体',
  relations: '写入关系',
  business: '写入业务资源',
  kg: '同步知识图谱',
  done: '已完成',
  failed: '失败',
};

const PHASE_COLOR: Record<string, string> = {
  done: 'green',
  failed: 'red',
  kg: 'purple',
  relations: 'geekblue',
  business: 'cyan',
};

interface Props {
  progress: ImportProgressSnapshot | null;
  /** 提交请求仍在等待时，即使还没有快照也显示进度条。 */
  active: boolean;
}

/** 导入提交进度条：阶段标签 + 百分比 + 服务端任务提示。 */
export default function ImportProgressBar({ progress, active }: Props) {
  if (!active && !progress) return null;

  const failed = Boolean(progress?.failed);
  const done = Boolean(progress?.done);
  const percent = Math.max(0, Math.min(100, Math.round(progress?.percent ?? 0)));
  const label = PHASE_LABEL[progress?.phase ?? ''] ?? progress?.phase ?? '准备中';
  const message = progress?.message || (active ? '正在提交导入任务…' : '');

  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space wrap size={8}>
        <Tag color={failed ? 'red' : done ? 'green' : PHASE_COLOR[progress?.phase ?? ''] ?? 'blue'}>
          {label}
        </Tag>
        <Typography.Text type={failed ? 'danger' : 'secondary'}>{message}</Typography.Text>
      </Space>
      <Progress
        percent={percent}
        status={failed ? 'exception' : done ? 'success' : 'active'}
        strokeColor={done ? undefined : failed ? undefined : { from: '#1677ff', to: '#52c41a' }}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        导入由服务端继续执行：切换到其他页面或刷新后返回，仍可在此查看进度和结果。
      </Typography.Text>
    </Space>
  );
}
