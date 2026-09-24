'use client';

import { Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import {
  App,
  Button,
  Empty,
  Modal,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd';
import {
  ArrowLeftOutlined,
  DeleteOutlined,
  LinkOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import {
  fetchHerbs,
  fetchLiteratures,
  fetchPrescriptions,
  fetchTheories,
  fetchKbResources,
  mountKbResource,
  unmountKbResource,
  fetchKnowledgeBases,
} from '@/services/api';
import { useRequestSeq } from '@/hooks/useRequestSeq';
import type {
  Herb,
  KBResource,
  KBResourceType,
  KnowledgeBase,
  Literature,
  Prescription,
  Theory,
} from '@/types';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';

const { Text } = Typography;

/** 资源类型中文标签与配色。 */
const RESOURCE_TYPE_META: Record<
  KBResourceType,
  { label: string; color: string }
> = {
  herb: { label: '中药', color: 'green' },
  prescription: { label: '方剂', color: 'blue' },
  theory: { label: '理论', color: 'purple' },
  literature: { label: '文献', color: 'orange' },
};

const RESOURCE_TYPE_OPTIONS = (Object.keys(RESOURCE_TYPE_META) as KBResourceType[]).map(
  (t) => ({
    value: t,
    label: RESOURCE_TYPE_META[t].label,
  }),
);

/** 资源选择项（统一形态：id + name）。 */
interface ResourceOption {
  id: string;
  name: string;
}

/** 按资源类型加载可选资源列表。 */
async function loadResourceOptions(
  type: KBResourceType,
): Promise<ResourceOption[]> {
  if (type === 'herb') {
    const resp = await fetchHerbs({ limit: 100 });
    return resp.items.map((h: Herb) => ({ id: h.id, name: h.name }));
  }
  if (type === 'prescription') {
    const resp = await fetchPrescriptions({ limit: 100 });
    return resp.items.map((p: Prescription) => ({ id: p.id, name: p.name }));
  }
  if (type === 'theory') {
    const resp = await fetchTheories({ limit: 100 });
    return resp.items.map((t: Theory) => ({ id: t.id, name: t.name }));
  }
  const resp = await fetchLiteratures({ limit: 100 });
  return resp.items.map((l: Literature) => ({ id: l.id, name: l.name }));
}

/** 内容组件：使用 useSearchParams，必须由 Suspense 包裹（Next.js prerender 要求）。 */
function KbResourcesContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const kbId = searchParams.get('kb_id') ?? '';
  const { message } = App.useApp();
  const reqSeq = useRequestSeq(); // BUG-048：丢弃过期的列表响应

  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [resources, setResources] = useState<KBResource[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [filterType, setFilterType] = useState<KBResourceType | undefined>();

  // 挂载弹窗
  const [mountOpen, setMountOpen] = useState(false);
  const [mountType, setMountType] = useState<KBResourceType>('herb');
  const [mountOptions, setMountOptions] = useState<ResourceOption[]>([]);
  const [mountOptionsLoading, setMountOptionsLoading] = useState(false);
  const [mountResourceId, setMountResourceId] = useState<string | undefined>();
  const [mountSubmitting, setMountSubmitting] = useState(false);

  const loadKb = useCallback(async () => {
    if (!kbId) return;
    try {
      // 后端无单 KB 详情端点，从列表中查找
      const list = await fetchKnowledgeBases();
      const k = list.find((x) => x.id === kbId) ?? null;
      if (!k) {
        message.error('知识库不存在或无权访问');
        router.push('/kb');
        return;
      }
      setKb(k);
    } catch {
      message.error('知识库信息加载失败');
    }
  }, [kbId, message, router]);

  const loadResources = useCallback(async () => {
    if (!kbId) return;
    // BUG-048：快速切换筛选条件时旧慢响应覆盖新数据 → 丢弃过期响应
    const reqId = reqSeq.begin();
    setLoading(true);
    try {
      const resp = await fetchKbResources(kbId, {
        resource_type: filterType,
        limit: 100,
      });
      if (!reqSeq.isLatest(reqId)) return;
      setResources(resp.items);
      setTotal(resp.total);
    } catch {
      if (reqSeq.isLatest(reqId)) message.error('挂载资源列表加载失败');
    } finally {
      if (reqSeq.isLatest(reqId)) setLoading(false);
    }
  }, [kbId, filterType, message, reqSeq]);

  useEffect(() => {
    void loadKb();
  }, [loadKb]);

  useEffect(() => {
    void loadResources();
  }, [loadResources]);

  // 切换挂载类型时加载对应资源列表
  useEffect(() => {
    if (!mountOpen) return;
    setMountResourceId(undefined);
    setMountOptionsLoading(true);
    loadResourceOptions(mountType)
      .then((opts) => setMountOptions(opts))
      .catch(() => {
        message.error('资源列表加载失败');
        setMountOptions([]);
      })
      .finally(() => setMountOptionsLoading(false));
  }, [mountOpen, mountType, message]);

  const onMount = async () => {
    if (!mountResourceId) {
      message.warning('请选择要挂载的资源');
      return;
    }
    setMountSubmitting(true);
    try {
      await mountKbResource(kbId, mountType, mountResourceId);
      message.success('资源挂载成功');
      setMountOpen(false);
      setMountResourceId(undefined);
      await loadResources();
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? '挂载失败';
      message.error(detail);
    } finally {
      setMountSubmitting(false);
    }
  };

  // BUG-049：columns 的 useMemo 依赖里包含本回调，必须稳定引用（useCallback），
  // 否则闭包会捕获旧版本的 loadResources/filterType（卸载后用旧条件刷新）。
  const onUnmount = useCallback(
    async (row: KBResource) => {
      try {
        await unmountKbResource(kbId, row.resource_type, row.resource_id);
        message.success(`「${row.resource_name}」已卸载`);
        await loadResources();
      } catch (err: unknown) {
        const detail =
          (err as { response?: { data?: { detail?: string } } })?.response?.data
            ?.detail ?? '卸载失败';
        message.error(detail);
      }
    },
    [kbId, message, loadResources],
  );

  const columns: ColumnsType<KBResource> = useMemo(
    () => [
      {
        title: '资源类型',
        dataIndex: 'resource_type',
        width: 120,
        render: (t: KBResourceType) => {
          const meta = RESOURCE_TYPE_META[t] ?? { label: t, color: 'default' };
          return <Tag color={meta.color}>{meta.label}</Tag>;
        },
        filters: RESOURCE_TYPE_OPTIONS.map((o) => ({
          text: o.label,
          value: o.value,
        })),
        onFilter: (val, row) => row.resource_type === val,
      },
      {
        title: '资源名称',
        dataIndex: 'resource_name',
        ellipsis: true,
        render: (name: string) => <Text strong>{name}</Text>,
      },
      {
        title: '资源 ID',
        dataIndex: 'resource_id',
        width: 280,
        ellipsis: true,
        render: (id: string) => <Text type="secondary" code>{id}</Text>,
      },
      {
        title: '挂载时间',
        dataIndex: 'created_at',
        width: 180,
        render: (t: string) => (t ? new Date(t).toLocaleString('zh-CN') : '-'),
      },
      {
        title: '操作',
        key: 'actions',
        width: 100,
        render: (_: unknown, row: KBResource) => (
          <Popconfirm
            title="确认卸载该资源？"
            description="卸载后该资源在本知识库中的向量将被删除。"
            okText="卸载"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => onUnmount(row)}
          >
            <Button type="text" danger size="small" icon={<DeleteOutlined />}>
              卸载
            </Button>
          </Popconfirm>
        ),
      },
    ],
    // BUG-049：依赖必须是闭包捕获到的回调（旧实现仅 [kbId] → 捕获旧的
    // onUnmount/loadResources，卸载后按旧筛选条件刷新列表）
    [onUnmount],
  );

  const headerLeft = (
    <Space>
      <Button
        type="text"
        icon={<ArrowLeftOutlined />}
        onClick={() => router.push('/kb')}
      />
      <h2 style={{ margin: 0 }}>
        {kb ? `「${kb.name}」资源挂载` : '知识库资源挂载'}
      </h2>
    </Space>
  );
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} onClick={() => void loadResources()}>
        刷新
      </Button>
      <Button
        type="primary"
        icon={<PlusOutlined />}
        onClick={() => setMountOpen(true)}
      >
        挂载资源
      </Button>
      <AdminHeaderRight />
    </Space>
  );

  if (!kbId) {
    return (
      <AppLayout pageTitle="" headerLeft={<h2 style={{ margin: 0 }}>知识库资源挂载</h2>} headerRight={<AdminHeaderRight />}>
        <Empty description="未指定知识库" />
      </AppLayout>
    );
  }

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      <Space style={{ marginBottom: 16 }}>
        <Text type="secondary">资源类型筛选：</Text>
        <Select
          allowClear
          style={{ width: 160 }}
          placeholder="全部类型"
          options={RESOURCE_TYPE_OPTIONS}
          value={filterType}
          onChange={(v) => setFilterType(v as KBResourceType | undefined)}
        />
      </Space>

      <Spin spinning={loading}>
        <Table<KBResource>
          rowKey={(r) => r.id}
          columns={columns}
          dataSource={resources}
          pagination={{
            total,
            pageSize: 100,
            showSizeChanger: false,
            showTotal: (t) => `共 ${t} 条`,
          }}
          locale={{ emptyText: <Empty description="暂无挂载资源" /> }}
        />
      </Spin>

      <Modal
        title="挂载结构化资源"
        open={mountOpen}
        onOk={onMount}
        onCancel={() => {
          setMountOpen(false);
          setMountResourceId(undefined);
        }}
        okText="挂载"
        cancelText="取消"
        confirmLoading={mountSubmitting}
        destroyOnClose
      >
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <div>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              资源类型
            </Text>
            <Select
              style={{ width: '100%' }}
              options={RESOURCE_TYPE_OPTIONS}
              value={mountType}
              onChange={(v) => setMountType(v as KBResourceType)}
            />
          </div>
          <div>
            <Text type="secondary" style={{ display: 'block', marginBottom: 8 }}>
              选择资源
            </Text>
            <Select
              showSearch
              style={{ width: '100%' }}
              placeholder="搜索资源名称"
              options={mountOptions.map((o) => ({
                value: o.id,
                label: o.name,
              }))}
              value={mountResourceId}
              loading={mountOptionsLoading}
              onChange={(v) => setMountResourceId(v as string | undefined)}
              filterOption={(input, option) =>
                (option?.label ?? '')
                  .toString()
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
              notFoundContent={
                mountOptionsLoading ? '加载中...' : '无可选资源'
              }
            />
          </div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            <LinkOutlined /> 挂载后该资源的结构化文本将被向量化写入知识库，
            可在问答中作为引用来源。
          </Text>
        </Space>
      </Modal>
    </AppLayout>
  );
}

export default function KbResourcesPage() {
  return (
    <Suspense fallback={null}>
      <KbResourcesContent />
    </Suspense>
  );
}
