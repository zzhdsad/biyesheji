'use client';

import { DeleteOutlined, FileAddOutlined, FolderOpenOutlined } from '@ant-design/icons';
import { Alert, Button, Empty, Input, List, Space, Tag, Typography } from 'antd';
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
 * 单文件选择允许多次追加，且**不去重**：不同目录下的同名文件路径相同，
 * 若在此静默丢弃其一，用户会以为两个文件都已选中。重复路径交由后端明确拒绝。
 */
export default function FilePicker({ onChange, accept, disabled }: Props) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dirInputRef = useRef<HTMLInputElement>(null);
  const [items, setItems] = useState<PickedFile[]>([]);

  const merge = useCallback((incoming: PickedFile[]) => {
    setItems((prev) => [...prev, ...incoming]);
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

  /** 路径重复 = 无法区分来源（单文件选择不带目录），必须让用户先处理。 */
  const duplicatePaths = useMemo(() => {
    const counts = new Map<string, number>();
    items.forEach((item) => counts.set(item.path, (counts.get(item.path) ?? 0) + 1));
    return Array.from(counts.entries())
      .filter(([, count]) => count > 1)
      .map(([path]) => path);
  }, [items]);

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

      {duplicatePaths.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="存在无法区分的同名文件"
          description={
            <>
              以下路径重复：{duplicatePaths.join('、')}。不同目录下的同名文件请改用
              「选择文件夹（递归）」保留目录结构，或重命名后再选；
              重复路径的提交会被后端拒绝，不会静默覆盖。
            </>
          }
        />
      )}

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
