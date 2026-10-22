'use client';

/**
 * 知识库回收站：批量删除入口 + 恢复 / 批量恢复 / 彻底删除 / 一键清空。
 *
 * 与 ResourceRecycleBin 的区别：知识库删除会级联影响文档、切片与向量，
 * 因此"彻底删除"前有更强提示；生命周期仍复用后端既有实现
 * （软删除 → 回收站 → purge 时清理 KB 挂载向量 + 文档向量）。
 */

import { useCallback, useEffect, useState } from 'react';
import { DeleteOutlined, RestOutlined, UndoOutlined } from '@ant-design/icons';
import {
  App,
  Badge,
  Button,
  Checkbox,
  Drawer,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tooltip,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';

import type { KnowledgeBase } from '@/types';
import {
  batchDeleteKbs,
  batchRestoreKbs,
  fetchKnowledgeBases,
  fetchTrashKbs,
  purgeAllKbTrash,
  restoreKb,
} from '@/services/api';

export interface KbRecycleBinProps {
  /** 回收站或删除操作后回调（刷新主列表）。 */
  onChanged?: () => void;
}

export default function KbRecycleBin({ onChanged }: KbRecycleBinProps) {
  const { message, modal } = App.useApp();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [trash, setTrash] = useState<KnowledgeBase[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  // 批量删除弹窗
  const [batchOpen, setBatchOpen] = useState(false);
  const [allKbs, setAllKbs] = useState<KnowledgeBase[]>([]);
  const [batchSelected, setBatchSelected] = useState<string[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setTrash(await fetchTrashKbs());
      setSelected([]);
    } catch {
      message.error('回收站加载失败');
    } finally {
      setLoading(false);
    }
  }, [message]);

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

  const onBatchRestore = async () => {
    setBusy(true);
    try {
      const res = await batchRestoreKbs(selected);
      afterChange(`已恢复 ${res.success ?? selected.length} 个知识库`);
    } catch (err) {
      message.error(`恢复失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  /** 一键清空：二次确认 + 后端再校验 admin；逐库清理向量，单库失败不影响其余。 */
  const onPurgeAll = () => {
    modal.confirm({
      title: '确认清空知识库回收站？',
      okText: '确认清空',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: (
        <div>
          <p>
            将彻底删除回收站中的 <b>{trash.length}</b> 个知识库及其下全部文档、切片与向量，
            <b>不可恢复</b>。
          </p>
          <Typography.Text type="secondary">
            建议先确认其中没有误删的真实知识库。
          </Typography.Text>
        </div>
      ),
      onOk: async () => {
        setBusy(true);
        try {
          const res = await purgeAllKbTrash();
          afterChange(
            res.failed?.length
              ? `已彻底删除 ${res.purged ?? 0} 个，失败 ${res.failed.length} 个`
              : `已彻底删除 ${res.purged ?? 0} 个知识库`,
          );
        } catch (err) {
          message.error(`清空失败：${(err as Error).message}`);
        } finally {
          setBusy(false);
        }
      },
    });
  };

  const openBatchDelete = async () => {
    setBatchOpen(true);
    try {
      setAllKbs(await fetchKnowledgeBases());
    } catch {
      message.error('知识库列表加载失败');
    }
  };

  const onBatchDelete = async () => {
    if (batchSelected.length === 0) return;
    setBusy(true);
    try {
      const res = await batchDeleteKbs(batchSelected);
      message.success(res.message ?? `已移入回收站 ${res.success ?? 0} 个`);
      if (res.failed?.length) {
        message.warning(`${res.failed.length} 个失败：${res.failed[0].reason}`);
      }
      setBatchOpen(false);
      setBatchSelected([]);
      onChanged?.();
      void load();
    } catch (err) {
      message.error(`批量删除失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnsType<KnowledgeBase> = [
    { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true },
    {
      title: '文档数',
      dataIndex: 'document_count',
      key: 'document_count',
      width: 90,
      render: (value?: number) => value ?? 0,
    },
    {
      title: '操作',
      key: 'action',
      width: 120,
      render: (_v, record) => (
        <Popconfirm
          title="确认恢复该知识库？"
          onConfirm={async () => {
            setBusy(true);
            try {
              await restoreKb(record.id);
              afterChange('已恢复');
            } catch (err) {
              message.error(`恢复失败：${(err as Error).message}`);
            } finally {
              setBusy(false);
            }
          }}
        >
          <Button size="small" icon={<UndoOutlined />} disabled={busy}>
            恢复
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <>
      <Space size="middle" align="center">
        <Tooltip title="选择多个知识库移入回收站">
          <Button danger icon={<DeleteOutlined />} onClick={() => void openBatchDelete()}>
            批量删除
          </Button>
        </Tooltip>
        <Tooltip title="查看/恢复/彻底删除知识库回收站">
          <Badge count={trash.length} size="small" offset={[-2, 2]}>
            <Button icon={<RestOutlined />} onClick={() => setOpen(true)}>
              回收站
            </Button>
          </Badge>
        </Tooltip>
      </Space>

      <Drawer
        title="知识库回收站"
        width={680}
        open={open}
        onClose={() => setOpen(false)}
        destroyOnHidden
        extra={
          <Space>
            <Button
              icon={<UndoOutlined />}
              disabled={busy || selected.length === 0}
              onClick={() => void onBatchRestore()}
            >
              批量恢复
            </Button>
            <Button
              danger
              type="primary"
              disabled={busy || trash.length === 0}
              onClick={onPurgeAll}
            >
              一键清空
            </Button>
          </Space>
        }
      >
        <Table<KnowledgeBase>
          rowKey="id"
          size="small"
          loading={loading || busy}
          dataSource={trash}
          columns={columns}
          pagination={false}
          locale={{ emptyText: '回收站为空' }}
          rowSelection={{ selectedRowKeys: selected, onChange: (k) => setSelected(k as string[]) }}
        />
      </Drawer>

      <Modal
        title="批量删除知识库"
        open={batchOpen}
        onCancel={() => setBatchOpen(false)}
        onOk={() => void onBatchDelete()}
        okText={`移入回收站（${batchSelected.length}）`}
        okButtonProps={{ danger: true, disabled: batchSelected.length === 0 }}
        cancelText="取消"
        confirmLoading={busy}
      >
        <Typography.Paragraph type="secondary">
          勾选要删除的知识库。删除后进入回收站，{/* 保留期由系统配置决定 */}可在回收站中恢复；
          彻底删除会同时清理其文档、切片与向量。
        </Typography.Paragraph>
        <div style={{ maxHeight: 320, overflowY: 'auto' }}>
          {allKbs.map((kb) => (
            <div key={kb.id} style={{ padding: '2px 0' }}>
              <Checkbox
                value={kb.id}
                checked={batchSelected.includes(kb.id)}
                onChange={(e) =>
                  setBatchSelected((cur) =>
                    e.target.checked ? [...cur, kb.id] : cur.filter((x) => x !== kb.id),
                  )
                }
              >
                {kb.name}
                <Typography.Text type="secondary" style={{ marginLeft: 8 }}>
                  文档 {kb.document_count ?? 0}
                </Typography.Text>
              </Checkbox>
            </div>
          ))}
        </div>
      </Modal>
    </>
  );
}
