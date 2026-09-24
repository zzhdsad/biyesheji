'use client';

import { useCallback, useEffect, useState } from 'react';
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
  TreeSelect,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TreeDataNode } from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  PlusOutlined,
  ReadOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import type { Category, Tag, Theory } from '@/types';
import {
  createTheory,
  deleteTheory,
  fetchCategories,
  fetchTags,
  fetchTheories,
  updateTheory,
} from '@/services/api';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import { useUserStore } from '@/stores/userStore';
import { useRequestSeq } from '@/hooks/useRequestSeq';

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

// ── 工具 ──────────────────────────────────────────────────────────────────────

/** 把后端嵌套分类转成 TreeSelect 数据。 */
function toTreeSelectData(nodes: Category[]): TreeDataNode[] {
  return nodes.map((node) => ({
    key: node.id,
    title: node.name,
    value: node.id,
    children: node.children?.length ? toTreeSelectData(node.children) : undefined,
  }));
}

/** 同时兼容 AppException {message} 与 HTTPException {detail}。 */
function pickErrorMessage(err: unknown, fallback: string): string {
  const data = (err as { response?: { data?: { message?: string; detail?: string } } })
    ?.response?.data;
  return data?.message || data?.detail || fallback;
}

/** 渲染别名色块。 */
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

interface TheoryFormValues {
  name: string;
  aliases: string[];
  category_id?: string | null;
  content: string;
  source: string;
  tag_ids: string[];
}

type ModalMode = 'create' | 'edit';

/** 列表查询所需的全部状态（setState 后立即查询时闭包内仍是旧值，需显式传入）。 */
interface QueryState {
  keyword: string;
  categoryId: string | undefined;
  tagId: string | undefined;
  current: number;
  pageSize: number;
}

// ── 页面 ──────────────────────────────────────────────────────────────────────

export default function TheoriesPage() {
  const { message } = App.useApp();
  const user = useUserStore((s) => s.user);
  const isAdmin = user?.role === 'admin';
  const reqSeq = useRequestSeq(); // BUG-048：丢弃过期的列表响应

  // 列表数据
  const [theories, setTheories] = useState<Theory[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [current, setCurrent] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  // 筛选
  const [keyword, setKeyword] = useState('');
  const [categoryId, setCategoryId] = useState<string | undefined>(undefined);
  const [tagId, setTagId] = useState<string | undefined>(undefined);

  // 分类树 / 标签选项
  const [categoryTree, setCategoryTree] = useState<Category[]>([]);
  const [allTags, setAllTags] = useState<Tag[]>([]);

  // 详情 Drawer
  const [detail, setDetail] = useState<Theory | null>(null);

  // 新建/编辑 Modal
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<ModalMode>('create');
  const [editTarget, setEditTarget] = useState<Theory | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<TheoryFormValues>();

  // ── 数据加载 ──────────────────────────────────────────────────────────────

  const loadTheories = async (opts?: QueryState) => {
    const kw = opts ? opts.keyword : keyword;
    const cat = opts ? opts.categoryId : categoryId;
    const tg = opts ? opts.tagId : tagId;
    const pg = opts ? opts.current : current;
    const ps = opts ? opts.pageSize : pageSize;
    // BUG-048：快速翻页/改条件时旧慢响应覆盖新数据与 total → 用序号丢弃过期响应
    const reqId = reqSeq.begin();
    setLoading(true);
    try {
      const resp = await fetchTheories({
        keyword: kw || undefined,
        category_id: cat,
        tag_id: tg,
        limit: ps,
        offset: (pg - 1) * ps,
      });
      if (!reqSeq.isLatest(reqId)) return;
      setTheories(resp.items);
      setTotal(resp.total);
    } catch (err) {
      if (reqSeq.isLatest(reqId)) message.error(pickErrorMessage(err, '理论列表加载失败'));
    } finally {
      if (reqSeq.isLatest(reqId)) setLoading(false);
    }
  };

  // BUG-073（lint）：被下面挂载 effect 依赖，改为 useCallback 固定引用（行为不变）
  const loadCategories = useCallback(async () => {
    try {
      const tree = await fetchCategories({ resource_type: 'theory', tree: true });
      setCategoryTree(tree);
    } catch {
      message.error('分类加载失败');
    }
  }, [message]);

  const loadTags = useCallback(async () => {
    try {
      const list = await fetchTags();
      setAllTags(list);
    } catch {
      message.error('标签加载失败');
    }
  }, [message]);

  useEffect(() => {
    void loadTheories();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, pageSize]);

  useEffect(() => {
    void loadCategories();
    void loadTags();
  }, [loadCategories, loadTags]);

  // ── 查询操作 ──────────────────────────────────────────────────────────────

  // BUG-048：旧实现 setCurrent(1) 之后又立刻 loadTheories()——非第 1 页时
  // 前者会触发 effect 再发一次请求，且后者用的还是旧页码与旧条件（双发 +
  // 数据错配）。改为：已在第 1 页则显式带条件查询一次，否则只切页码由 effect 加载。
  const onSearch = () => {
    if (current === 1) {
      void loadTheories({ keyword, categoryId, tagId, current: 1, pageSize });
    } else {
      setCurrent(1);
    }
  };

  const onReset = () => {
    setKeyword('');
    setCategoryId(undefined);
    setTagId(undefined);
    if (current === 1) {
      void loadTheories({
        keyword: '',
        categoryId: undefined,
        tagId: undefined,
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
      category_id: null,
      content: '',
      source: '',
      tag_ids: [],
    });
    setModalOpen(true);
  };

  const openEdit = (theory: Theory) => {
    setModalMode('edit');
    setEditTarget(theory);
    setModalOpen(true);
  };

  const handleModalOpenChange = (open: boolean) => {
    if (open && editTarget) {
      // 与 herbs/page.tsx 一致：等 Modal/Form mount 后再回填，规避 StrictMode 双挂载导致字段丢失
      requestAnimationFrame(() => {
        form.setFieldsValue({
          name: editTarget.name,
          aliases: editTarget.aliases,
          category_id: editTarget.category_id,
          content: editTarget.content,
          source: editTarget.source,
          tag_ids: editTarget.tags.map((t) => t.id),
        });
      });
    }
  };

  const onSubmit = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);

      const payload = {
        name: values.name.trim(),
        aliases: values.aliases ?? [],
        category_id: values.category_id ?? null,
        content: values.content ?? '',
        source: values.source ?? '',
        tag_ids: values.tag_ids ?? [],
      };

      if (modalMode === 'edit' && editTarget) {
        await updateTheory(editTarget.id, payload);
        message.success('理论已更新');
      } else {
        await createTheory(payload);
        message.success('理论已创建');
      }

      setModalOpen(false);
      form.resetFields();
      setEditTarget(null);
      await loadTheories();
    } catch (err) {
      const detail = pickErrorMessage(err, '');
      if (detail) message.error(detail);
      // 空 detail 为表单校验失败，antd 已提示
    } finally {
      setSubmitting(false);
    }
  };

  const onDelete = async (theory: Theory) => {
    try {
      await deleteTheory(theory.id);
      message.success(`理论「${theory.name}」已删除`);
      // 若当前页只剩这一条且非首页，回退一页
      if (theories.length === 1 && current > 1) {
        setCurrent(current - 1);
      } else {
        await loadTheories();
      }
    } catch (err) {
      message.error(pickErrorMessage(err, '删除失败'));
    }
  };

  // ── 表格列 ────────────────────────────────────────────────────────────────

  const columns: ColumnsType<Theory> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 180,
      ellipsis: true,
      render: (v: string) => <Typography.Text strong>{v}</Typography.Text>,
    },
    {
      title: '别名',
      dataIndex: 'aliases',
      key: 'aliases',
      width: 200,
      render: (v: string[]) => <TagList items={v} />,
    },
    {
      title: '分类',
      dataIndex: 'category',
      key: 'category',
      width: 120,
      render: (_: unknown, row: Theory) => row.category?.name ?? '—',
    },
    {
      title: '标签',
      dataIndex: 'tags',
      key: 'tags',
      width: 200,
      render: (_: unknown, row: Theory) =>
        row.tags.length === 0 ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <Space size={[4, 4]} wrap>
            {row.tags.map((t) => (
              <AntTag key={t.id} color={t.color || undefined}>
                {t.name}
              </AntTag>
            ))}
          </Space>
        ),
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
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 170,
      render: (v: string) =>
        v ? new Date(v).toLocaleString('zh-CN') : '—',
    },
    {
      title: '操作',
      key: 'action',
      width: isAdmin ? 200 : 90,
      fixed: 'right',
      render: (_: unknown, row: Theory) => (
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
                title={`确认删除理论「${row.name}」？`}
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
      <ReadOutlined style={{ fontSize: 20 }} />
      <h2 style={{ margin: 0 }}>中医理论</h2>
    </Space>
  );
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} onClick={() => void loadTheories()}>
        刷新
      </Button>
      {isAdmin && (
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建理论
        </Button>
      )}
      <AdminHeaderRight />
    </Space>
  );

  // ── 标签 Select 选项 ──────────────────────────────────────────────────────

  const tagOptions = allTags.map((t) => ({
    label: (
      <Space size={6}>
        <span
          style={{
            display: 'inline-block',
            width: 12,
            height: 12,
            borderRadius: 3,
            background: t.color || 'transparent',
            border: '1px solid rgba(128,128,128,0.4)',
          }}
        />
        {t.name}
      </Space>
    ),
    value: t.id,
  }));

  const formInitial: TheoryFormValues = {
    name: '',
    aliases: [],
    category_id: null,
    content: '',
    source: '',
    tag_ids: [],
  };

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      {/* 查询区 */}
      <Card style={{ marginBottom: 16 }}>
        <Space size="middle" wrap>
          <Input.Search
            placeholder="搜索理论名称、别名、内容、来源"
            allowClear
            style={{ width: 280 }}
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onSearch={onSearch}
            prefix={<SearchOutlined />}
          />
          <TreeSelect
            placeholder="按分类筛选"
            allowClear
            style={{ width: 220 }}
            treeData={toTreeSelectData(categoryTree)}
            treeDefaultExpandAll
            value={categoryId}
            onChange={(v) => setCategoryId(v ?? undefined)}
          />
          <Select
            placeholder="按标签筛选"
            allowClear
            style={{ width: 220 }}
            options={tagOptions}
            value={tagId}
            onChange={(v) => setTagId(v ?? undefined)}
          />
          <Button type="primary" onClick={onSearch}>
            查询
          </Button>
          <Button onClick={onReset}>重置</Button>
        </Space>
      </Card>

      {/* 列表 */}
      <Table<Theory>
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={theories}
        loading={loading}
        scroll={{ x: 1200 }}
        locale={{ emptyText: '暂无理论数据' }}
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
        title="理论详情"
        open={detail !== null}
        onClose={() => setDetail(null)}
        width={640}
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
            <Descriptions.Item label="分类">
              {detail.category?.name ?? '—'}
            </Descriptions.Item>
            <Descriptions.Item label="来源">
              {detail.source || '—'}
            </Descriptions.Item>
            <Descriptions.Item label="内容">
              {detail.content ? (
                <Typography.Paragraph
                  style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                >
                  {detail.content}
                </Typography.Paragraph>
              ) : (
                <Typography.Text type="secondary">—</Typography.Text>
              )}
            </Descriptions.Item>
            <Descriptions.Item label="标签">
              <Space size={[4, 4]} wrap>
                {detail.tags.length === 0 ? (
                  <Typography.Text type="secondary">—</Typography.Text>
                ) : (
                  detail.tags.map((t) => (
                    <AntTag key={t.id} color={t.color || undefined}>
                      {t.name}
                    </AntTag>
                  ))
                )}
              </Space>
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
        title={modalMode === 'edit' ? '编辑理论' : '新建理论'}
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
        width={720}
      >
        <Form<TheoryFormValues>
          form={form}
          layout="vertical"
          initialValues={formInitial}
          preserve={false}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入理论名称' },
              { whitespace: true, message: '名称不能为纯空格' },
              { max: 128, message: '最长 128 字' },
            ]}
          >
            <Input placeholder="如：阴阳学说" autoFocus />
          </Form.Item>

          <Form.Item name="aliases" label="别名（可回车添加多个）">
            <Select
              mode="tags"
              placeholder="如：阴阳、太极"
              tokenSeparators={[',', '，']}
            />
          </Form.Item>

          <Form.Item name="category_id" label="分类">
            <TreeSelect
              treeData={toTreeSelectData(categoryTree)}
              allowClear
              placeholder="选择理论分类"
              treeDefaultExpandAll
            />
          </Form.Item>

          <Form.Item
            name="content"
            label="内容"
            rules={[{ max: 50000, message: '最长 50000 字' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 4, maxRows: 12 }}
              placeholder="理论的详细内容"
              showCount
            />
          </Form.Item>

          <Form.Item
            name="source"
            label="来源"
            rules={[{ max: 255, message: '最长 255 字' }]}
          >
            <Input placeholder="如：《素问·阴阳应象大论》" />
          </Form.Item>

          <Form.Item name="tag_ids" label="标签">
            <Select
              mode="multiple"
              placeholder="选择标签"
              options={tagOptions}
              allowClear
            />
          </Form.Item>
        </Form>
      </Modal>
    </AppLayout>
  );
}
