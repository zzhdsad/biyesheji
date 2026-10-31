'use client';

import { DeleteOutlined, FileAddOutlined, FolderOpenOutlined } from '@ant-design/icons';
import { Button, Empty, Input, List, Space, Tag, Typography } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export interface PickedFile {
  file: File;
  /** 相对路径：单文件时为文件名，选择文件夹时为目录内的相对路径 */
  path: string;
}

interface Props {
  onChange: (items: PickedFile[]) => void;
  accept?: string;
  disabled?: boolean;
}

/**
 * 多文件 + 文件夹递归选择器。
 *
 * 文件夹递归依赖 <input webkitdirectory>，一次选目录即可拿到全部层级文件，
 * 并用 webkitRelativePath 保留目录结构（后端据此还原相对路径溯源）。
 * 单文件选择允许多次追加，按相对路径去重。
 */
export default function FilePicker({ onChange, accept, disabled }: Props) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dirInputRef = useRef<HTMLInputElement>(null);
  const [items, setItems] = useState<PickedFile[]>([]);

  const merge = useCallback((incoming: PickedFile[]) => {
    setItems((prev) => {
      const seen = new Set(prev.map((i) => i.path));
      const merged = [...prev];
      incoming.forEach((item) => {
        if (!seen.has(item.path)) {
          seen.add(item.path);
          merged.push(item);
        }
      });
      return merged;
    });
  }, []);

  const onPickFiles = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      merge(files.map((file) => ({ file, path: file.name })));
      event.target.value = '';
    },
    [merge],
  );

  const onPickDirectory = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      merge(
        files.map((file) => ({
          file,
          path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
        })),
      );
      event.target.value = '';
    },
    [merge],
  );

  useEffect(() => {
    onChange(items);
  }, [items, onChange]);

  const totalSize = useMemo(
    () => items.reduce((sum, item) => sum + item.file.size, 0),
    [items],
  );

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Space wrap>
        <Button
          icon={<FileAddOutlined />}
          disabled={disabled}
          onClick={() => fileInputRef.current?.click()}
        >
          选择文件（可多选）
        </Button>
        <Button
          icon={<FolderOpenOutlined />}
          disabled={disabled}
          onClick={() => dirInputRef.current?.click()}
        >
          选择文件夹（递归）
        </Button>
        <Button
          icon={<DeleteOutlined />}
          disabled={disabled || items.length === 0}
          onClick={() => setItems([])}
        >
          清空
        </Button>
        <Typography.Text type="secondary">
          已选 {items.length} 个文件，共 {(totalSize / 1024).toFixed(1)} KB
        </Typography.Text>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={accept}
          hidden
          onChange={onPickFiles}
        />
        {/* eslint-disable-next-line react/no-unknown-property */}
        <input
          ref={dirInputRef}
          type="file"
          hidden
          multiple
          onChange={onPickDirectory}
          {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
        />
      </Space>

      {items.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="尚未选择文件；支持 PDF/DOCX/TXT/MD/CSV/TSV/JSON/JSONL/XLSX/Parquet"
        />
      ) : (
        <List
          size="small"
          bordered
          dataSource={items}
          style={{ maxHeight: 260, overflowY: 'auto' }}
          renderItem={(item, index) => (
            <List.Item
              actions={[
                <Button
                  key="remove"
                  type="text"
                  size="small"
                  icon={<DeleteOutlined />}
                  onClick={() => setItems((prev) => prev.filter((_, i) => i !== index))}
                />,
              ]}
            >
              <Space size="small">
                <Tag>{item.file.name.split('.').pop()?.toUpperCase()}</Tag>
                <Typography.Text code ellipsis style={{ maxWidth: 420 }}>
                  {item.path}
                </Typography.Text>
                <Typography.Text type="secondary">
                  {(item.file.size / 1024).toFixed(1)} KB
                </Typography.Text>
              </Space>
            </List.Item>
          )}
        />
      )}
      <Input.TextArea
        readOnly
        hidden
        value={items.map((i) => i.path).join('\n')}
        aria-hidden
      />
    </Space>
  );
}
