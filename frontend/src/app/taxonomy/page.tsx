'use client';

import { useEffect, useState } from 'react';
import {
  App,
  Button,
  Card,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Segmented,
  Space,
  Table,
  Tabs,
  Tree,
  TreeSelect,
  Typography,
} from 'antd';
import type { TreeDataNode } from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import type { Category, Tag, TaxonomyResourceType } from '@/types';
import {
  createCategory,
  createTag,
  deleteCategory,
  deleteTag,
  fetchCategories,
  fetchTags,
  updateCategory,
  updateTag,
} from '@/services/api';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import ResourceRecycleBin from '@/components/admin/ResourceRecycleBin';

// ── 常量 ────────────────────────────────────────────────────────────────────

const RESOURCE_OPTIONS: { label: string; value: TaxonomyResourceType }[] = [
  { label: '中药', value: 'herb' },
  { label: '方剂', value: 'prescription' },
  { label: '中医理论', value: 'theory' },
  { label: '文献', value: 'literature' },
];

const RESOURCE_LABEL: Record<TaxonomyResourceType, string> = {
  herb: '中药',
  prescription: '方剂',
  theory: '中医理论',
  literature: '文献',
};

// ── 类型 ────────────────────────────────────────────────────────────────────

interface CategoryFormValues {
  resource_type: TaxonomyResourceType;
  parent_id?: string | null;
  name: string;
  sort_order: number;
  description: string;
}

interface TagFormValues {
  name: string;
  color: string;
  description: string;
}

type CategoryModalMode = 'create-root' | 'create-child' | 'edit';

interface CategoryModalState {
  mode: CategoryModalMode;
  target?: Category;
  initial: CategoryFormValues;
}

// ── 树工具 ──────────────────────────────────────────────────────────────────

/** 转成 antd TreeSelect 数据。 */
function toTreeSelectData(nodes: Category[]): TreeDataNode[] {
  return nodes.map((node) => ({
    key: node.id,
    title: node.name,
    value: node.id,
    children: node.children?.length ? toTreeSelectData(node.children) : undefined,
  }));
}

/** 提取 axios 错误中的后端 message。 */
function pickErrorMessage(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { message?: string } } })
    ?.response?.data?.message;
  return detail || fallback;
}

// ── 页面 ────────────────────────────────────────────────────────────────────

export default function TaxonomyPage() {
  const { message } = App.useApp();

  // ── 分类状态 ──
  const [resourceType, setResourceType] =
    useState<TaxonomyResourceType>('herb');
  const [categoryTree, setCategoryTree] = useState<Category[]>([]);
  const [categoryLoading, setCategoryLoading] = useState(false);
  const [categoryModal, setCategoryModal] = useState<CategoryModalState | null>(
    null,
  );
  const [categorySubmitting, setCategorySubmitting] = useState(false);
  const [categoryForm] = Form.useForm<CategoryFormValues>();

  // ── 标签状态 ──
  const [tags, setTags] = useState<Tag[]>([]);
  const [tagLoading, setTagLoading] = useState(false);
  const [tagKeyword, setTagKeyword] = useState('');
  const [tagModal, setTagModal] = useState<{
    mode: 'create' | 'edit';
    target?: Tag;
    initial: TagFormValues;
  } | null>(null);
  const [tagSubmitting, setTagSubmitting] = useState(false);
  const [tagForm] = Form.useForm<TagFormValues>();

  // ── 数据加载 ──────────────────────────────────────────────────────────────

  const loadCategories = async (type: TaxonomyResourceType) => {
    setCategoryLoading(true);
    try {
      const tree = await fetchCategories({ resource_type: type, tree: true });
      setCategoryTree(tree);
    } catch {
      message.error('分类加载失败');
    } finally {
      setCategoryLoading(false);
    }
  };

  const loadTags = async (keyword?: string) => {
    setTagLoading(true);
    try {
      const list = await fetchTags(keyword || undefined);
      setTags(list);
    } catch {
      message.error('标签加载失败');
    } finally {
      setTagLoading(false);
    }
  };

  /** 回收站操作后刷新分类与标签。 */
  const loadAll = async () => {
    await Promise.all([loadCategories(resourceType), loadTags()]);
  };

  useEffect(() => {
    void loadCategories(resourceType);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resourceType]);

  useEffect(() => {
    void loadTags();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── 分类弹窗 ──────────────────────────────────────────────────────────────

  const openCreateRoot = () => {
    setCategoryModal({
      mode: 'create-root',
      initial: {
        resource_type: resourceType,
        parent_id: null,
        name: '',
        sort_order: 0,
        description: '',
      },
    });
  };

  const openCreateChild = (parent: Category) => {
    setCategoryModal({
      mode: 'create-child',
      target: parent,
      initial: {
        resource_type: parent.resource_type,
        parent_id: parent.id,
        name: '',
        sort_order: 0,
        description: '',
      },
    });
  };

  const openEdit = (node: Category) => {
    setCategoryModal({
      mode: 'edit',
      target: node,
      initial: {
        resource_type: node.resource_type,
        parent_id: node.parent_id,
        name: node.name,
        sort_order: node.sort_order,
        description: node.description,
      },
    });
  };

  const onCategorySubmit = async () => {
    if (!categoryModal) return;
    try {
      const values = await categoryForm.validateFields();
      setCategorySubmitting(true);

      if (categoryModal.mode === 'edit' && categoryModal.target) {
        await updateCategory(categoryModal.target.id, {
          name: values.name,
          sort_order: values.sort_order,
          description: values.description,
        });
        message.success('分类已更新');
      } else {
        const parentId =
          categoryModal.mode === 'create-child'
            ? categoryModal.target?.id ?? null
            : values.parent_id ?? null;
        await createCategory({
          resource_type: resourceType,
          parent_id: parentId,
          name: values.name,
          sort_order: values.sort_order,
          description: values.description,
        });
        message.success('分类已创建');
      }

      setCategoryModal(null);
      categoryForm.resetFields();
      await loadCategories(resourceType);
    } catch (err) {
      const detail = pickErrorMessage(err, '');
      if (detail) message.error(detail);
      // 无 detail 时为表单校验失败，antd 已提示
    } finally {
      setCategorySubmitting(false);
    }
  };

  const onCategoryDelete = async (node: Category) => {
    try {
      await deleteCategory(node.id);
      message.success(`分类「${node.name}」已删除`);
      await loadCategories(resourceType);
    } catch (err) {
      message.error(pickErrorMessage(err, '删除失败'));
    }
  };

  /** 渲染树节点标题：名称 + 排序 + 操作按钮。 */
  const renderTreeTitle = (node: Category) => (
    <Space size={8} style={{ paddingRight: 8 }}>
      <Typography.Text strong>{node.name}</Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        排序 {node.sort_order}
      </Typography.Text>
      {node.description && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }} ellipsis>
          {node.description}
        </Typography.Text>
      )}
      <Button
        type="text"
        size="small"
        icon={<PlusOutlined />}
        onClick={(e) => {
          e.stopPropagation();
          openCreateChild(node);
        }}
      >
        子分类
      </Button>
      <Button
        type="text"
        size="small"
        icon={<EditOutlined />}
        onClick={(e) => {
          e.stopPropagation();
          openEdit(node);
        }}
      />
      <Popconfirm
        title={`确认删除分类「${node.name}」？`}
        okText="删除"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        onConfirm={() => onCategoryDelete(node)}
      >
        <Button
          type="text"
          size="small"
          danger
          icon={<DeleteOutlined />}
          onClick={(e) => e.stopPropagation()}
        />
      </Popconfirm>
    </Space>
  );

  const toTreeData = (nodes: Category[]): TreeDataNode[] =>
    nodes.map((node) => ({
      key: node.id,
      title: renderTreeTitle(node),
      children: node.children?.length ? toTreeData(node.children) : undefined,
    }));

  // ── 标签弹窗 ──────────────────────────────────────────────────────────────

  const onTagSubmit = async () => {
    if (!tagModal) return;
    try {
      const values = await tagForm.validateFields();
      setTagSubmitting(true);

      if (tagModal.mode === 'edit' && tagModal.target) {
        await updateTag(tagModal.target.id, values);
        message.success('标签已更新');
      } else {
        await createTag(values);
        message.success('标签已创建');
      }

      setTagModal(null);
      tagForm.resetFields();
      await loadTags(tagKeyword || undefined);
    } catch (err) {
      const detail = pickErrorMessage(err, '');
      if (detail) message.error(detail);
    } finally {
      setTagSubmitting(false);
    }
  };

  const onTagDelete = async (tag: Tag) => {
    try {
      await deleteTag(tag.id);
      message.success(`标签「${tag.name}」已删除`);
      await loadTags(tagKeyword || undefined);
    } catch (err) {
      message.error(pickErrorMessage(err, '删除失败'));
    }
  };

  // ── 头部 ──────────────────────────────────────────────────────────────────

  const headerLeft = <h2 style={{ margin: 0 }}>分类与标签</h2>;
  // 分类 / 标签各自的回收站（恢复 / 彻底删除 / 一键清空），权限由后端二次校验
const headerRight = (
  <Space size="middle" align="center">
    <ResourceRecycleBin
      resourceType="category"
      label="分类"
      onChanged={() => void loadAll()}
    />
    <ResourceRecycleBin resourceType="tag" label="标签" onChanged={() => void loadAll()} />
    <AdminHeaderRight />
  </Space>
);

  // 父节点选择仅"新建根分类"时可改；子分类/编辑时固定
  const parentSelectDisabled = categoryModal?.mode !== 'create-root';

  const tagColumns = [
    {
      title: '标签',
      dataIndex: 'name',
      render: (_: unknown, record: Tag) => (
        <Space>
          <Typography.Text strong>{record.name}</Typography.Text>
          {record.color ? (
            <span
              style={{
                display: 'inline-block',
                width: 14,
                height: 14,
                borderRadius: 3,
                background: record.color,
                border: '1px solid rgba(128,128,128,0.4)',
              }}
            />
          ) : null}
        </Space>
      ),
    },
    { title: '颜色', dataIndex: 'color', width: 160 },
    {
      title: '描述',
      dataIndex: 'description',
      render: (v: string) => v || <Typography.Text type="secondary">—</Typography.Text>,
    },
    { title: '创建时间', dataIndex: 'created_at', width: 200 },
    {
      title: '操作',
      width: 140,
      render: (_: unknown, record: Tag) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<EditOutlined />}
            onClick={() =>
              setTagModal({
                mode: 'edit',
                target: record,
                initial: {
                  name: record.name,
                  color: record.color,
                  description: record.description,
                },
              })
            }
          />
          <Popconfirm
            title={`确认删除标签「${record.name}」？`}
            okText="删除"
            okButtonProps={{ danger: true }}
            cancelText="取消"
            onConfirm={() => onTagDelete(record)}
          >
            <Button type="text" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      <Tabs
        defaultActiveKey="categories"
        items={[
          {
            key: 'categories',
            label: '分类管理',
            children: (
              <Card
                bordered={false}
                title={
                  <Space>
                    <span>资源类型</span>
                    <Segmented
                      options={RESOURCE_OPTIONS}
                      value={resourceType}
                      onChange={(v) =>
                        setResourceType(v as TaxonomyResourceType)
                      }
                    />
                  </Space>
                }
                extra={
                  <Space>
                    <Button
                      icon={<ReloadOutlined />}
                      onClick={() => void loadCategories(resourceType)}
                    >
                      刷新
                    </Button>
                    <Button
                      type="primary"
                      icon={<PlusOutlined />}
                      onClick={openCreateRoot}
                    >
                      新建根分类
                    </Button>
                  </Space>
                }
              >
                {categoryTree.length === 0 ? (
                  <Empty description={`暂无${RESOURCE_LABEL[resourceType]}分类`} />
                ) : (
                  <Tree
                    showLine
                    defaultExpandAll
                    blockNode
                    treeData={toTreeData(categoryTree)}
                  />
                )}
              </Card>
            ),
          },
          {
            key: 'tags',
            label: '标签管理',
            children: (
              <Card
                bordered={false}
                title={
                  <Input
                    prefix={<SearchOutlined />}
                    placeholder="按标签名搜索，回车查询"
                    allowClear
                    style={{ width: 300 }}
                    value={tagKeyword}
                    onChange={(e) => setTagKeyword(e.target.value)}
                    onPressEnter={() => void loadTags(tagKeyword)}
                  />
                }
                extra={
                  <Space>
                    <Button onClick={() => void loadTags(tagKeyword)}>刷新</Button>
                    <Button
                      type="primary"
                      icon={<PlusOutlined />}
                      onClick={() =>
                        setTagModal({
                          mode: 'create',
                          initial: { name: '', color: '', description: '' },
                        })
                      }
                    >
                      新建标签
                    </Button>
                  </Space>
                }
              >
                <Table
                  rowKey="id"
                  loading={tagLoading}
                  dataSource={tags}
                  columns={tagColumns}
                  pagination={{ pageSize: 20, showSizeChanger: false }}
                />
              </Card>
            ),
          },
        ]}
      />

      {/* ── 分类弹窗 ── */}
      <Modal
        title={
          categoryModal?.mode === 'edit'
            ? '编辑分类'
            : categoryModal?.mode === 'create-child'
              ? `在「${categoryModal.target?.name}」下新建子分类`
              : `新建${RESOURCE_LABEL[resourceType]}根分类`
        }
        open={categoryModal !== null}
        onOk={onCategorySubmit}
        onCancel={() => {
          setCategoryModal(null);
          categoryForm.resetFields();
        }}
        okText="保存"
        cancelText="取消"
        confirmLoading={categorySubmitting}
        destroyOnClose
      >
        {categoryModal && (
          <Form<CategoryFormValues>
            form={categoryForm}
            layout="vertical"
            initialValues={categoryModal.initial}
            preserve={false}
          >
            <Form.Item label="资源类型">
              <Input value={RESOURCE_LABEL[resourceType]} disabled />
            </Form.Item>
            <Form.Item name="parent_id" label="父分类（不选为根分类）">
              <TreeSelect
                treeData={toTreeSelectData(categoryTree)}
                allowClear
                placeholder="不选为根分类"
                treeDefaultExpandAll
                disabled={parentSelectDisabled}
              />
            </Form.Item>
            <Form.Item
              name="name"
              label="分类名称"
              rules={[
                { required: true, message: '请输入分类名称' },
                { max: 64, message: '最长 64 字' },
              ]}
            >
              <Input placeholder="如：解表药" autoFocus />
            </Form.Item>
            <Form.Item name="sort_order" label="同级排序（数字越小越靠前）">
              <InputNumber min={0} precision={0} style={{ width: '100%' }} />
            </Form.Item>
            <Form.Item name="description" label="描述（可选）">
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                maxLength={2000}
                showCount
              />
            </Form.Item>
          </Form>
        )}
      </Modal>

      {/* ── 标签弹窗 ── */}
      <Modal
        title={tagModal?.mode === 'edit' ? '编辑标签' : '新建标签'}
        open={tagModal !== null}
        onOk={onTagSubmit}
        onCancel={() => {
          setTagModal(null);
          tagForm.resetFields();
        }}
        okText="保存"
        cancelText="取消"
        confirmLoading={tagSubmitting}
        destroyOnClose
      >
        {tagModal && (
          <Form<TagFormValues>
            form={tagForm}
            layout="vertical"
            initialValues={tagModal.initial}
            preserve={false}
          >
            <Form.Item
              name="name"
              label="标签名称"
              rules={[
                { required: true, message: '请输入标签名称' },
                { max: 32, message: '最长 32 字' },
              ]}
            >
              <Input placeholder="如：补气" autoFocus />
            </Form.Item>
            <Form.Item
              name="color"
              label="标签颜色（可选，Ant Design 颜色名或色值）"
            >
              <Input placeholder="如：gold / #d4c4a8" />
            </Form.Item>
            <Form.Item name="description" label="描述（可选）">
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                maxLength={2000}
                showCount
              />
            </Form.Item>
          </Form>
        )}
      </Modal>
    </AppLayout>
  );
}
