'use client';

import { useEffect, useState } from 'react';
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
  Spin,
  Table,
  Tag as AntTag,
  TreeSelect,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TreeDataNode } from 'antd';
import {
  BookOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import type { Category, Literature, Tag } from '@/types';
import {
  createLiterature,
  deleteLiterature,
  fetchCategories,
  fetchLiteratures,
  fetchTags,
  getLiterature,
  updateLiterature,
} from '@/services/api';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import { useUserStore } from '@/stores/userStore';
import { useRequestSeq } from '@/hooks/useRequestSeq';

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;
const MAX_ALIASES = 20;
const MAX_TAGS = 20;

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

/** 文本为空时显示 —。 */
function TextOrDash({ value }: { value: string }) {
  return value ? <>{value}</> : <Typography.Text type="secondary">—</Typography.Text>;
}

// ── 表单值 ────────────────────────────────────────────────────────────────────

interface LiteratureFormValues {
  name: string;
  aliases: string[];
  category_id?: string | null;
  author: string;
  dynasty: string;
  summary: string;
  content: string;
  source: string;
  tag_ids: string[];
}

type ModalMode = 'create' | 'edit';

// ── 页面 ──────────────────────────────────────────────────────────────────────

export default function LiteraturesPage() {
  const { message } = App.useApp();
  const user = useUserStore((s) => s.user);
  const isAdmin = user?.role === 'admin';
  const reqSeq = useRequestSeq(); // BUG-048：丢弃过期的列表响应

  // 列表数据
  const [literatures, setLiteratures] = useState<Literature[]>([]);
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

  // 详情 Drawer（详情独立通过 GET /literatures/{id} 获取）
  const [detail, setDetail] = useState<Literature | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // 新建/编辑 Modal
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<ModalMode>('create');
  const [editTarget, setEditTarget] = useState<Literature | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<LiteratureFormValues>();

  // ── 数据加载 ──────────────────────────────────────────────────────────────

  const loadLiteratures = async (overrides?: {
    keyword?: string;
    categoryId?: string;
    tagId?: string;
    current?: number;
    pageSize?: number;
  }) => {
    // overrides 用于重置等场景：state 已 set 但闭包尚未更新时，显式传入新值
    const useKeyword = overrides?.keyword ?? keyword;
    const useCategoryId =
      overrides && 'categoryId' in overrides ? overrides.categoryId : categoryId;
    const useTagId = overrides && 'tagId' in overrides ? overrides.tagId : tagId;
    const useCurrent = overrides?.current ?? current;
    const usePageSize = overrides?.pageSize ?? pageSize;
    // BUG-048：快速翻页/改条件时旧慢响应覆盖新数据与 total → 丢弃过期响应
    const reqId = reqSeq.begin();
    setLoading(true);
    try {
      const offset = (useCurrent - 1) * usePageSize;
      const resp = await fetchLiteratures({
        keyword: useKeyword || undefined,
        category_id: useCategoryId,
        tag_id: useTagId,
        limit: usePageSize,
        offset,
      });
      if (!reqSeq.isLatest(reqId)) return;
      setLiteratures(resp.items);
      setTotal(resp.total);
    } catch (err) {
      if (reqSeq.isLatest(reqId)) message.error(pickErrorMessage(err, '文献列表加载失败'));
    } finally {
      if (reqSeq.isLatest(reqId)) setLoading(false);
    }
  };

  const loadCategories = async () => {
    try {
      const tree = await fetchCategories({ resource_type: 'literature', tree: true });
      setCategoryTree(tree);
    } catch {
      message.error('分类加载失败');
    }
  };

  const loadTags = async () => {
    try {
      const list = await fetchTags();
      setAllTags(list);
    } catch {
      message.error('标签加载失败');
    }
  };

  useEffect(() => {
    void loadLiteratures();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, pageSize]);

  useEffect(() => {
    void loadCategories();
    void loadTags();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── 查询操作 ──────────────────────────────────────────────────────────────

  // BUG-048：旧实现 setCurrent(1) 之后又立刻查询——非第 1 页时 effect 会再发
  // 一次请求（双发）。改为：已在第 1 页则显式带条件查询，否则只切页码由 effect 加载。
  const onSearch = () => {
    if (current === 1) {
      void loadLiteratures({ keyword, categoryId, tagId, current: 1, pageSize });
    } else {
      setCurrent(1);
    }
  };

  const onReset = () => {
    setKeyword('');
    setCategoryId(undefined);
    setTagId(undefined);
    if (current === 1) {
      // 显式传入空条件，避免 setState 后闭包内仍是旧筛选值
      void loadLiteratures({
        keyword: '',
        categoryId: undefined,
        tagId: undefined,
        current: 1,
      });
    } else {
      setCurrent(1);
    }
  };

  // ── 详情 ──────────────────────────────────────────────────────────────────

  const openDetail = async (literature: Literature) => {
    setDetail(literature); // 先用列表行渲染标题区，再请求完整详情
    setDetailLoading(true);
    try {
      const full = await getLiterature(literature.id);
      setDetail(full);
    } catch (err) {
      message.error(pickErrorMessage(err, '文献详情加载失败'));
    } finally {
      setDetailLoading(false);
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
      author: '',
      dynasty: '',
      summary: '',
      content: '',
      source: '',
      tag_ids: [],
    });
    setModalOpen(true);
  };

  const openEdit = (literature: Literature) => {
    setModalMode('edit');
    setEditTarget(literature);
    setModalOpen(true);
  };

  const handleModalOpenChange = (open: boolean) => {
    if (open && editTarget) {
      // 与 theories/page.tsx 一致：等 Modal/Form mount 后再回填，规避 StrictMode 双挂载导致字段丢失
      requestAnimationFrame(() => {
        form.setFieldsValue({
          name: editTarget.name,
          aliases: editTarget.aliases,
          category_id: editTarget.category_id,
          author: editTarget.author,
          dynasty: editTarget.dynasty,
          summary: editTarget.summary,
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

      // tag_ids 始终完整提交：清空标签时必须发送 []，不能被 Form 忽略
      const payload = {
        name: values.name.trim(),
        aliases: values.aliases ?? [],
        category_id: values.category_id ?? null,
        author: values.author ?? '',
        dynasty: values.dynasty ?? '',
        summary: values.summary ?? '',
        content: values.content ?? '',
        source: values.source ?? '',
        tag_ids: values.tag_ids ?? [],
      };

      if (modalMode === 'edit' && editTarget) {
        await updateLiterature(editTarget.id, payload);
        message.success('文献已更新');
      } else {
        await createLiterature(payload);
        message.success('文献已创建');
      }

      setModalOpen(false);
      form.resetFields();
      setEditTarget(null);
      // 创建后回到第一页；编辑保持当前查询条件与页码
      if (modalMode === 'create') {
        setCurrent(1);
      }
      await loadLiteratures();
    } catch (err) {
      const detail = pickErrorMessage(err, '');
      if (detail) message.error(detail);
      // 空 detail 为表单校验失败，antd 已提示
    } finally {
      setSubmitting(false);
    }
  };

  const onDelete = async (literature: Literature) => {
    try {
      await deleteLiterature(literature.id);
      message.success(`文献「${literature.name}」已删除`);
      // 若当前页只剩这一条且非首页，回退一页
      if (literatures.length === 1 && current > 1) {
        setCurrent(current - 1);
      } else {
        await loadLiteratures();
      }
    } catch (err) {
      message.error(pickErrorMessage(err, '删除失败'));
    }
  };

  // ── 表格列 ────────────────────────────────────────────────────────────────

  const columns: ColumnsType<Literature> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      width: 180,
      ellipsis: true,
      render: (v: string, row) => (
        <Typography.Link
          strong
          onClick={() => void openDetail(row)}
          style={{ whiteSpace: 'normal' }}
        >
          {v}
        </Typography.Link>
      ),
    },
    {
      title: '作者',
      dataIndex: 'author',
      key: 'author',
      width: 120,
      ellipsis: true,
      render: (v: string) => <TextOrDash value={v} />,
    },
    {
      title: '朝代',
      dataIndex: 'dynasty',
      key: 'dynasty',
      width: 90,
      render: (v: string) => <TextOrDash value={v} />,
    },
    {
      title: '分类',
      dataIndex: 'category',
      key: 'category',
      width: 120,
      render: (_: unknown, row: Literature) => row.category?.name ?? '—',
    },
    {
      title: '标签',
      dataIndex: 'tags',
      key: 'tags',
      width: 180,
      render: (_: unknown, row: Literature) =>
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
      render: (v: string) => <TextOrDash value={v} />,
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
      render: (_: unknown, row: Literature) => (
        <Space size={4}>
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => void openDetail(row)}
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
                title="确定删除该文献吗？"
                description={`将永久删除「${row.name}」`}
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
      <BookOutlined style={{ fontSize: 20 }} />
      <h2 style={{ margin: 0 }}>中医文献</h2>
    </Space>
  );
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} onClick={() => void loadLiteratures()}>
        刷新
      </Button>
      {isAdmin && (
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新增文献
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

  const formInitial: LiteratureFormValues = {
    name: '',
    aliases: [],
    category_id: null,
    author: '',
    dynasty: '',
    summary: '',
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
            placeholder="搜索名称、别名、作者、朝代、摘要、正文、来源"
            allowClear
            style={{ width: 320 }}
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
      <Table<Literature>
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={literatures}
        loading={loading}
        scroll={{ x: 1200 }}
        locale={{ emptyText: '暂无文献数据' }}
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
        title="文献详情"
        open={detail !== null}
        onClose={() => setDetail(null)}
        width={680}
        destroyOnClose
      >
        <Spin spinning={detailLoading}>
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
              <Descriptions.Item label="作者">
                <TextOrDash value={detail.author} />
              </Descriptions.Item>
              <Descriptions.Item label="朝代">
                <TextOrDash value={detail.dynasty} />
              </Descriptions.Item>
              <Descriptions.Item label="摘要">
                {detail.summary ? (
                  <Typography.Paragraph
                    style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                  >
                    {detail.summary}
                  </Typography.Paragraph>
                ) : (
                  <Typography.Text type="secondary">—</Typography.Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="正文">
                {detail.content ? (
                  <Typography.Paragraph
                    style={{
                      whiteSpace: 'pre-wrap',
                      marginBottom: 0,
                      maxHeight: 360,
                      overflowY: 'auto',
                    }}
                  >
                    {detail.content}
                  </Typography.Paragraph>
                ) : (
                  <Typography.Text type="secondary">—</Typography.Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="来源">
                <TextOrDash value={detail.source} />
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
        </Spin>
      </Drawer>

      {/* 新建/编辑 Modal */}
      <Modal
        title={modalMode === 'edit' ? '编辑文献' : '新增文献'}
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
        width={760}
      >
        <Form<LiteratureFormValues>
          form={form}
          layout="vertical"
          initialValues={formInitial}
          preserve={false}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入文献名称' },
              { whitespace: true, message: '名称不能为纯空格' },
              { max: 128, message: '最长 128 字' },
            ]}
          >
            <Input placeholder="如：黄帝内经" autoFocus />
          </Form.Item>

          <Form.Item
            name="aliases"
            label="别名（可回车添加多个，最多 20 个）"
            rules={[
              {
                validator: (_, v: string[]) =>
                  !v || v.length <= MAX_ALIASES
                    ? Promise.resolve()
                    : Promise.reject(new Error(`别名最多 ${MAX_ALIASES} 个`)),
              },
            ]}
          >
            <Select
              mode="tags"
              placeholder="如：内经、素问"
              tokenSeparators={[',', '，']}
            />
          </Form.Item>

          <Form.Item name="category_id" label="分类">
            <TreeSelect
              treeData={toTreeSelectData(categoryTree)}
              allowClear
              placeholder="选择文献分类"
              treeDefaultExpandAll
            />
          </Form.Item>

          <Space size="middle" style={{ display: 'flex' }} align="start">
            <Form.Item
              name="author"
              label="作者"
              style={{ flex: 1 }}
              rules={[{ max: 255, message: '最长 255 字' }]}
            >
              <Input placeholder="如：张仲景" />
            </Form.Item>
            <Form.Item
              name="dynasty"
              label="朝代"
              style={{ flex: 1 }}
              rules={[{ max: 32, message: '最长 32 字' }]}
            >
              <Input placeholder="如：东汉" />
            </Form.Item>
          </Space>

          <Form.Item
            name="summary"
            label="摘要"
            rules={[{ max: 500, message: '最长 500 字' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 5 }}
              placeholder="文献内容摘要"
              showCount
            />
          </Form.Item>

          <Form.Item
            name="content"
            label="正文"
            rules={[{ max: 50000, message: '最长 50000 字' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 5, maxRows: 14 }}
              placeholder="文献正文或精选段落"
              showCount
            />
          </Form.Item>

          <Form.Item
            name="source"
            label="来源"
            rules={[{ max: 255, message: '最长 255 字' }]}
          >
            <Input placeholder="如：人民卫生出版社校注本" />
          </Form.Item>

          <Form.Item
            name="tag_ids"
            label="标签（最多 20 个）"
            rules={[
              {
                validator: (_, v: string[]) =>
                  !v || v.length <= MAX_TAGS
                    ? Promise.resolve()
                    : Promise.reject(new Error(`标签最多 ${MAX_TAGS} 个`)),
              },
            ]}
          >
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
