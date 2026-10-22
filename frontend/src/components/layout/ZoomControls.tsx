'use client';

import { Button, Space, Tooltip, theme } from 'antd';
import {
  CompressOutlined,
  ExpandOutlined,
  ReloadOutlined,
} from '@ant-design/icons';

/** 全站缩放按钮（配合 AppLayout 的 useUiZoom）。 */
export function ZoomControls({
  zoom,
  onZoomIn,
  onZoomOut,
  onReset,
}: {
  zoom: number;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onReset: () => void;
}) {
  const { token } = theme.useToken();
  return (
    <div className="ui-zoom-controls" role="group" aria-label="页面缩放">
      <Space.Compact>
        <Tooltip title="缩小（Ctrl + 滚轮向下）">
          <Button
            size="small"
            icon={<CompressOutlined />}
            onClick={onZoomOut}
            aria-label="缩小"
          />
        </Tooltip>
        <Tooltip title="恢复 100%">
          <Button
            size="small"
            onClick={onReset}
            style={{ minWidth: 54, color: token.colorTextSecondary }}
            aria-label="恢复缩放"
          >
            {zoom}%
          </Button>
        </Tooltip>
        <Tooltip title="放大（Ctrl + 滚轮向上）">
          <Button
            size="small"
            icon={<ExpandOutlined />}
            onClick={onZoomIn}
            aria-label="放大"
          />
        </Tooltip>
      </Space.Compact>
      <Tooltip title="重置">
        <Button
          size="small"
          type="text"
          icon={<ReloadOutlined />}
          onClick={onReset}
          aria-label="重置缩放"
        />
      </Tooltip>
    </div>
  );
}
