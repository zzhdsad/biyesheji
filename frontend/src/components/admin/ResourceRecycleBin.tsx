'use client';

/**
 * 管理中心统一「回收站」抽屉：单条/批量恢复、单条/批量彻底删除、一键清空。
 *
 * 所有资源模块（中药 / 方剂 / 中医理论 / 文献 / 分类 / 标签）复用本组件，
 * 生命周期完全由后端 src/application/trash_service.py 统一实现：
 *   删除 → 回收站（软删除） → 恢复 / 彻底删除（清理向量与关联）
 * 前端只负责交互与二次确认；权限由后端再次校验（不依赖按钮是否展示）。
 */

import { useCallback, useEffect, useState } from 'react';
import { DeleteOutlined, RestOutlined, UndoOutlined } from '@ant-design/icons';
import { App, Badge, Button, Drawer, Popconfirm, Space, Table, Tooltip, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

import {
  batchPurgeResources,
  batchRestoreResources,
  fetchResourceTrash,
  purgeAllResourceTrash,
  purgeResource,
  restoreResource,
  type TrashResourceType,
} from '@/services/api';

/** 回收站条目（后端各资源 ToOut 的公共子集）。 */
interface TrashItem {
  id: string;
  name?: string;
  deleted_at?: string | null;
  [key: string]: unknown;
}

export interface ResourceRecycleBinProps {
  /** 资源类型（决定后端路径与向量清理方式）。 */
  resourceType: TrashResourceType;
  /** 中文名，用于提示文案，如「中药」。 */
  label: string;
  /** 回收站内容变化后回调（用于刷新主列表）。 */
  onChanged?: () => void;
  /** 无权限时隐藏入口（后端仍会二次校验）。 */
  disabled?: boolean;
}

function formatTime(value?: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN');
}

export default function ResourceRecycleBin({
  resourceType,
  label,
  onChanged,
  disabled = false,
}: ResourceRecycleBinProps) {
  const { message, modal } = App.useApp();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [items, setItems] = useState<TrashItem[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchResourceTrash<TrashItem>(resourceType, { limit: 100, offset: 0 });
      setItems(res.items ?? []);
      setTotal(res.total ?? 0);
      setSelectedKeys([]);
    } catch {
      message.error('回收站加载失败');
    } finally {
      setLoading(false);
    }
  }, [message, resourceType]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const afterChange = useCallback(
    (msg: string) => {
      message.success(msg);
      void load();
      onChanged?.();
    },
    [message, load, onChanged],
  );

  const onRestore = async (id: string) => {
    setBusy(true);
    try {
      await restoreResource(resourceType, id);
      afterChange('已恢复');
    } catch (err) {
      message.error(`恢复失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const onPurge = async (id: string) => {
    setBusy(true);
    try {
      await purgeResource(resourceType, id);
      afterChange('已彻底删除');
    } catch (err) {
      message.error(`彻底删除失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const onBatchRestore = async () => {
    setBusy(true);
    try {
      const res = await batchRestoreResources(resourceType, selectedKeys);
      afterChange(`已恢复 ${res.success ?? selectedKeys.length} 条`);
    } catch (err) {
      message.error(`批量恢复失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const onBatchPurge = async () => {
    setBusy(true);
    try {
      const res = await batchPurgeResources(resourceType, selectedKeys);
      const failed = res.failed?.length ?? 0;
      afterChange(
        failed
          ? `已彻底删除 ${res.purged ?? 0} 条，失败 ${failed} 条`
          : `已彻底删除 ${res.purged ?? 0} 条`,
      );
    } catch (err) {
      message.error(`批量彻底删除失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  /** 一键清空：危险操作，必须二次确认（后端仍会再次校验 admin 权限）。 */
  const onPurgeAll = () => {
    modal.confirm({
      title: `确认清空「${label}」回收站？`,
      okText: '确认清空',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: (
        <div>
          <p>
            将彻底删除回收站中的 <b>{total}</b> 条{label}记录（含其知识库挂载与向量数据），
            <b>不可恢复</b>。
          </p>
          <Typography.Text type="secondary">
            建议先确认其中没有误删的真实数据。
          </Typography.Text>
        </div>
      ),
      onOk: async () => {
        setBusy(true);
        try {
          const res = await purgeAllResourceTrash(resourceType);
          afterChange(`已清空回收站，彻底删除 ${res.purged ?? 0} 条`);
        } catch (err) {
          message.error(`清空失败：${(err as Error).message}`);
        } finally {
          setBusy(false);
        }
      },
    });
  };

  const columns: ColumnsType<TrashItem> = [
    { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true },
    {
      title: '删除时间',
      dataIndex: 'deleted_at',
      key: 'deleted_at',
      width: 190,
      render: (value: string | null) => formatTime(value),
    },
    {
      title: '操作',
      key: 'action',
      width: 180,
      render: (_value, record) => (
        <Space size={4}>
          <Popconfirm title="确认恢复？" onConfirm={() => onRestore(record.id)}>
            <Button size="small" icon={<UndoOutlined />} disabled={busy}>
              恢复
            </Button>
          </Popconfirm>
          <Popconfirm
            title="确认彻底删除？"
            description="删除后不可恢复，且会清理向量与挂载。"
            okButtonProps={{ danger: true }}
            okText="彻底删除"
            onConfirm={() => onPurge(record.id)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} disabled={busy}>
              彻底删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  if (disabled) return null;

  return (
    <>
      <Tooltip title={`查看/恢复/彻底删除${label}回收站`}>
        <Badge count={total} size="small" offset={[-2, 2]}>
          <Button icon={<RestOutlined />} onClick={() => setOpen(true)}>
            回收站
          </Button>
        </Badge>
      </Tooltip>
      <Drawer
        title={`${label}回收站`}
        width={720}
        open={open}
        onClose={() => setOpen(false)}
        destroyOnHidden
        extra={
          <Space>
            <Button
              icon={<UndoOutlined />}
              disabled={busy || selectedKeys.length === 0}
              onClick={() => void onBatchRestore()}
            >
              批量恢复
            </Button>
            <Popconfirm
              title={`确认彻底删除选中的 ${selectedKeys.length} 条？`}
              okButtonProps={{ danger: true }}
              okText="彻底删除"
              onConfirm={() => void onBatchPurge()}
            >
              <Button danger icon={<DeleteOutlined />} disabled={busy || selectedKeys.length === 0}>
                批量彻底删除
              </Button>
            </Popconfirm>
            <Button danger type="primary" disabled={busy || total === 0} onClick={onPurgeAll}>
              一键清空
            </Button>
          </Space>
        }
      >
        <Table<TrashItem>
          rowKey="id"
          size="small"
          loading={loading || busy}
          dataSource={items}
          columns={columns}
          pagination={false}
          locale={{ emptyText: '回收站为空' }}
          rowSelection={{
            selectedRowKeys: selectedKeys,
            onChange: (keys) => setSelectedKeys(keys as string[]),
          }}
        />
      </Drawer>
    </>
  );
}
