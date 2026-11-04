'use client';

import { useEffect, useRef, useState } from 'react';
import {
  App,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag as AntTag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import {
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  MedicineBoxOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import type { Herb } from '@/types';
import {
  batchDeleteResources,
  createHerb,
  deleteHerb,
  fetchHerbs,
  updateHerb,
} from '@/services/api';
import { TranslatableField } from '@/components/common/TranslatableField';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import ResourceRecycleBin from '@/components/admin/ResourceRecycleBin';
import { useUserStore } from '@/stores/userStore';
import { useRequestSeq } from '@/hooks/useRequestSeq';

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

// ── 工具 ──────────────────────────────────────────────────────────────────────

/** 同时兼容 AppException {message} 与 HTTPException {detail}。 */
function pickErrorMessage(err: unknown, fallback: string): string {
  const data = (err as { response?: { data?: { message?: string; detail?: string } } })
    ?.response?.data;
  return data?.message || data?.detail || fallback;
}

/** 渲染标签色块 + 名称。 */
function TagList({ items }: { items: string[] }) {
  if (!items || items.length === 0) {
    return <Typography.Text type="secondary">—</Typography.Text>;
  }
  return (
    <Space size={[4, 4]} wrap>
      {items.map((t) => (
        <AntTag key={t}>{t}</AntTag>
      ))}
    </Space>
  );
}

// ── 表单值 ────────────────────────────────────────────────────────────────────

interface HerbFormValues {
  name: string;
  aliases: string[];
  properties: string;
  channels: string[];
  effects: string;
  source: string;
  description: string;
}

type ModalMode = 'create' | 'edit';

/** 列表查询所需的全部状态。 */
interface QueryState {
  keyword: string;
  current: number;
  pageSize: number;
}

// ── 页面 ──────────────────────────────────────────────────────────────────────

export default function HerbsPage() {
  const { message } = App.useApp();
  const user = useUserStore((s) => s.user);
  const isAdmin = user?.role === 'admin';
  const reqSeq = useRequestSeq(); // BUG-048：丢弃过期的列表响应

  // 列表数据
  const [herbs, setHerbs] = useState<Herb[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [current, setCurrent] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  // 筛选
  const [keyword, setKeyword] = useState('');
  const initialQueryRef = useRef(false);

  // 详情 Drawer
  const [detail, setDetail] = useState<Herb | null>(null);

  // 新建/编辑 Modal
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<ModalMode>('create');
  const [editTarget, setEditTarget] = useState<Herb | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 批量选择（批量删除 → 回收站）
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [form] = Form.useForm<HerbFormValues>();

  // ── 数据加载 ──────────────────────────────────────────────────────────────

  const loadHerbs = async (opts?: QueryState) => {
    const kw = opts ? opts.keyword : keyword;
    const pg = opts ? opts.current : current;
    const ps = opts ? opts.pageSize : pageSize;
    // BUG-048：快速翻页/改条件时旧慢响应覆盖新数据与 total → 丢弃过期响应
    const reqId = reqSeq.begin();
    setLoading(true);
    try {
      const resp = await fetchHerbs({
        keyword: kw || undefined,
        limit: ps,
        offset: (pg - 1) * ps,
      });
      if (!reqSeq.isLatest(reqId)) return;
      setHerbs(resp.items);
      setTotal(resp.total);
    } catch (err) {
      if (reqSeq.isLatest(reqId)) message.error(pickErrorMessage(err, '中药列表加载失败'));
    } finally {
      if (reqSeq.isLatest(reqId)) setLoading(false);
    }
  };

  useEffect(() => {
    // 从问答引用“定位来源”进入：?keyword=xxx 自动按名称检索
    const opts = !initialQueryRef.current
      ? (() => {
          initialQueryRef.current = true;
          const kw = new URLSearchParams(window.location.search).get('keyword');
          if (!kw) return undefined;
          setKeyword(kw);
          return { keyword: kw, current: 1, pageSize };
        })()
      : undefined;
    void loadHerbs(opts);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, pageSize]);

  // ── 查询操作 ──────────────────────────────────────────────────────────────

  const onSearch = () => {
    if (current === 1) {
      void loadHerbs({
        keyword,
        current: 1,
        pageSize,
      });
    } else {
      // 翻页触发 effect，effect 闭包读取到的已是最新筛选 state
      setCurrent(1);
    }
  };

  const onReset = () => {
    setKeyword('');
    if (current === 1) {
      void loadHerbs({
        keyword: '',
        current: 1,
        pageSize,
      });
    } else {
      setCurrent(1);
    }
  };

  // ── 新建 / 编辑 ────────────────────────────────────────────────────────────

  const openCreate = () => {
    setModalMode('create');
    setEditTarget(null);
    form.resetFields();
    form.setFieldsValue({
      name: '',
      aliases: [],
      properties: '',
      channels: [],
      effects: '',
      source: '',
      description: '',
    });
    setModalOpen(true);
  };

  const openEdit = (herb: Herb) => {
    setModalMode('edit');
    setEditTarget(herb);
    setModalOpen(true);
  };

  const handleModalOpenChange = (open: boolean) => {
    if (open && editTarget) {
      // 与 users/page.tsx 一致：等 Modal/Form mount 后再回填
      requestAnimationFrame(() => {
        form.setFieldsValue({
          name: editTarget.name,
          aliases: editTarget.aliases,
          properties: editTarget.properties,
          channels: editTarget.channels,
          effects: editTarget.effects,
          source: editTarget.source,
          description: editTarget.description,
        });
      });
    }
  };

  const onSubmit = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);

      const payload = {
        name: values.name,
        aliases: values.aliases ?? [],
        properties: values.properties ?? '',
        channels: values.channels ?? [],
        effects: values.effects ?? '',
        source: values.source ?? '',
        description: values.description ?? '',
      };

      if (modalMode === 'edit' && editTarget) {
        await updateHerb(editTarget.id, payload);
        message.success('中药已更新');
      } else {
        await createHerb(payload);
        message.success('中药已创建');
      }

      setModalOpen(false);
      form.resetFields();
      setEditTarget(null);
      await loadHerbs();
    } catch (err) {
      const detail = pickErrorMessage(err, '');
      if (detail) message.error(detail);
      // 空 detail 为表单校验失败，antd 已提示
    } finally {
      setSubmitting(false);
    }
  };

  /** 批量删除：选中项移入回收站（后端二次校验 admin，保留期内可恢复）。 */
  const onBatchDelete = async () => {
    if (selectedRowKeys.length === 0) return;
    try {
      const res = await batchDeleteResources('herb', selectedRowKeys as string[]);
      message.success(res.message ?? `已移入回收站 ${res.success ?? 0} 条`);
      if (res.failed?.length) {
        message.warning(`${res.failed.length} 条失败：${res.failed[0].reason}`);
      }
      setSelectedRowKeys([]);
      await loadHerbs();
    } catch (err) {
      message.error(pickErrorMessage(err, '批量删除失败'));
    }
  };

  const onDelete = async (herb: Herb) => {
    try {
      await deleteHerb(herb.id);
      message.success(`中药「${herb.name}」已移入回收站`);
      // 若当前页只剩这一条且非首页，回退一页
      if (herbs.length === 1 && current > 1) {
        setCurrent(current - 1);
      } else {
        await loadHerbs();
      }
    } catch (err) {
      message.error(pickErrorMessage(err, '删除失败'));
    }
  };

  // ── 表格列 ────────────────────────────────────────────────────────────────

  const columns: ColumnsType<Herb> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 160,
      ellipsis: true,
      render: (v: string) => <Typography.Text strong>{v}</Typography.Text>,
    },
    {
      title: '别名',
      dataIndex: 'aliases',
      key: 'aliases',
      width: 180,
      render: (v: string[]) => <TagList items={v} />,
    },
    {
      title: '性味',
      dataIndex: 'properties',
      key: 'properties',
      width: 120,
      render: (v: string) => v || '—',
    },
    {
      title: '归经',
      dataIndex: 'channels',
      key: 'channels',
      width: 180,
      render: (v: string[]) => <TagList items={v} />,
    },
    {
      title: '功效',
      dataIndex: 'effects',
      key: 'effects',
      width: 200,
      ellipsis: true,
      render: (v: string) => v || '—',
    },
    {
      title: '来源',
      dataIndex: 'source',
      key: 'source',
      width: 160,
      ellipsis: true,
      render: (v: string) => v || '—',
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (v: string) =>
        v ? new Date(v).toLocaleString('zh-CN') : '—',
    },
    {
      title: '操作',
      key: 'action',
      width: isAdmin ? 200 : 90,
      fixed: 'right',
      render: (_: unknown, row: Herb) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => setDetail(row)}
          >
            查看
          </Button>
          {isAdmin && (
            <>
              <Button
                size="small"
                icon={<EditOutlined />}
                onClick={() => openEdit(row)}
              >
                编辑
              </Button>
              <Popconfirm
                title={`确认删除中药「${row.name}」？`}
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                onConfirm={() => void onDelete(row)}
              >
                <Button size="small" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            </>
          )}
        </Space>
      ),
    },
  ];

  // ── Header ────────────────────────────────────────────────────────────────

  const headerLeft = (
    <Space size="middle" align="center">
      <MedicineBoxOutlined style={{ fontSize: 20 }} />
      <h2 style={{ margin: 0 }}>中药管理</h2>
    </Space>
  );
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} onClick={() => void loadHerbs()}>
        刷新
      </Button>
      {isAdmin && (
        <Popconfirm
          title={`确认删除选中的 ${selectedRowKeys.length} 条中药？`}
          description="将移入回收站，保留期内可恢复。"
          okText="移入回收站"
          cancelText="取消"
          onConfirm={() => void onBatchDelete()}
        >
          <Button danger icon={<DeleteOutlined />} disabled={selectedRowKeys.length === 0}>
            批量删除{selectedRowKeys.length ? `（${selectedRowKeys.length}）` : ''}
          </Button>
        </Popconfirm>
      )}
      {/* 回收站：恢复 / 彻底删除 / 一键清空（权限由后端二次校验） */}
      <ResourceRecycleBin resourceType="herb" label="中药" onChanged={() => void loadHerbs()} />
      {isAdmin && (
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建中药
        </Button>
      )}
      <AdminHeaderRight />
    </Space>
  );

  const formInitial: HerbFormValues = {
    name: '',
    aliases: [],
    properties: '',
    channels: [],
    effects: '',
    source: '',
    description: '',
  };

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      {/* 查询区 */}
      <Card style={{ marginBottom: 16 }}>
        <Space size="middle" wrap>
          <Input.Search
            placeholder="按名称/别名/功效等关键词搜索"
            allowClear
            style={{ width: 280 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={onSearch}
            prefix={<SearchOutlined />}
          />
          <Button type="primary" onClick={onSearch}>
            查询
          </Button>
          <Button onClick={onReset}>重置</Button>
        </Space>
      </Card>

      {/* 列表 */}
      <Table<Herb>
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={herbs}
        loading={loading}
        rowSelection={
          isAdmin
            ? {
                selectedRowKeys,
                onChange: (keys) => setSelectedRowKeys(keys),
              }
            : undefined
        }
        scroll={{ x: 1400 }}
        locale={{ emptyText: '暂无中药数据' }}
        pagination={{
          current,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (t) => `共 ${t} 条`,
        }}
        onChange={(pag) => {
          if (pag.current !== current) setCurrent(pag.current as number);
          if (pag.pageSize !== pageSize) {
            setPageSize(pag.pageSize as number);
            setCurrent(1);
          }
        }}
      />

      {/* 详情 Drawer */}
      <Drawer
        title="中药详情"
        open={detail !== null}
        onClose={() => setDetail(null)}
        width={560}
        destroyOnClose
      >
        {detail && (
          <Descriptions column={1} bordered size="small">
            <Descriptions.Item label="名称">
              {detail.name}
            </Descriptions.Item>
            <Descriptions.Item label="别名">
              <TagList items={detail.aliases} />
            </Descriptions.Item>
            <Descriptions.Item label="性味">
              <TranslatableField label="性味" value={detail.properties} />
            </Descriptions.Item>
            <Descriptions.Item label="归经">
              <TagList items={detail.channels} />
            </Descriptions.Item>
            <Descriptions.Item label="功效">
              <TranslatableField label="功效" value={detail.effects} />
            </Descriptions.Item>
            <Descriptions.Item label="来源">
              <TranslatableField label="来源" value={detail.source} />
            </Descriptions.Item>
            <Descriptions.Item label="描述">
              <TranslatableField label="描述" value={detail.description} />
            </Descriptions.Item>
            <Descriptions.Item label="创建时间">
              {detail.created_at
                ? new Date(detail.created_at).toLocaleString('zh-CN')
                : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="更新时间">
              {detail.updated_at
                ? new Date(detail.updated_at).toLocaleString('zh-CN')
                : '—'}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Drawer>

      {/* 新建/编辑 Modal */}
      <Modal
        title={modalMode === 'edit' ? '编辑中药' : '新建中药'}
        open={modalOpen}
        afterOpenChange={handleModalOpenChange}
        onOk={onSubmit}
        onCancel={() => {
          setModalOpen(false);
          form.resetFields();
          setEditTarget(null);
        }}
        okText="保存"
        cancelText="取消"
        confirmLoading={submitting}
        destroyOnClose
        width={640}
      >
        <Form<HerbFormValues>
          form={form}
          layout="vertical"
          initialValues={formInitial}
          preserve={false}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入中药名称' },
              { max: 128, message: '最长 128 字' },
            ]}
          >
            <Input placeholder="如：金银花" autoFocus />
          </Form.Item>

          <Form.Item name="aliases" label="别名（可回车添加多个）">
            <Select
              mode="tags"
              placeholder="如：忍冬花、双花"
              tokenSeparators={[',', '，']}
            />
          </Form.Item>

          <Form.Item
            name="properties"
            label="性味"
            rules={[{ max: 255, message: '最长 255 字' }]}
          >
            <Input placeholder="如：苦，寒" />
          </Form.Item>

          <Form.Item name="channels" label="归经（可回车添加多个）">
            <Select
              mode="tags"
              placeholder="如：肺经、胃经"
              tokenSeparators={[',', '，']}
            />
          </Form.Item>

          <Form.Item
            name="effects"
            label="功效"
            rules={[{ max: 5000, message: '最长 5000 字' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 4 }}
              placeholder="如：清热解毒"
            />
          </Form.Item>

          <Form.Item
            name="source"
            label="来源"
            rules={[{ max: 255, message: '最长 255 字' }]}
          >
            <Input placeholder="如：《中国药典》" />
          </Form.Item>

          <Form.Item
            name="description"
            label="描述"
            rules={[{ max: 10000, message: '最长 10000 字' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 6 }}
              placeholder="综合描述"
              showCount
            />
          </Form.Item>
        </Form>
      </Modal>
    </AppLayout>
  );
}
