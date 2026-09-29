'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  App,
  Button,
  Card,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag as AntTag,
  TreeSelect,
  Typography,
} from 'antd';
import type { FormInstance } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { TreeDataNode } from 'antd';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  PlusOutlined,
  ProfileOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import type {
  Category,
  Herb,
  Prescription,
  PrescriptionIngredient,
  Tag,
} from '@/types';
import {
  batchDeleteResources,
  createPrescription,
  deletePrescription,
  fetchCategories,
  fetchHerbs,
  fetchPrescriptions,
  fetchTags,
  updatePrescription,
} from '@/services/api';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import ResourceRecycleBin from '@/components/admin/ResourceRecycleBin';
import { useUserStore } from '@/stores/userStore';
import { useRequestSeq } from '@/hooks/useRequestSeq';

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

/** 「未填写分类」哨兵值（后端解析为 category_id IS NULL）。 */
const UNCATEGORIZED_VALUE = '__none__';

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

/** 组成摘要：最多列前 3 味，如“麻黄、桂枝、杏仁等 5 味”。 */
function ingredientSummary(row: Prescription): string {
  // BUG-054：后端可能返回 null（历史数据/缺省字段）→ 展开前兜底，否则崩溃
  const list = [...(row.ingredients ?? [])].sort((a, b) => a.sort_order - b.sort_order);
  if (list.length === 0) return '—';
  const top = list.slice(0, 3).map((i) => i.herb_name).filter(Boolean);
  if (list.length <= 3) return top.join('、');
  return `${top.join('、')}等 ${list.length} 味`;
}

// ── 表单值 ────────────────────────────────────────────────────────────────────

/** Form.List 单行的本地结构（比后端多保留 herb_name 仅用于回显）。 */
interface IngredientFormItem {
  herb_id?: string;
  herb_name?: string;
  amount?: number | null;
  unit?: string;
  processing?: string;
  role?: string;
}

interface PrescriptionFormValues {
  name: string;
  aliases: string[];
  category_id?: string | null;
  efficacy: string;
  indications: string;
  usage_method: string;
  source: string;
  description: string;
  ingredients: IngredientFormItem[];
  tag_ids: string[];
}

type ModalMode = 'create' | 'edit';

/** 列表查询所需的全部状态。 */
interface QueryState {
  keyword: string;
  categoryId: string | undefined;
  tagId: string | undefined;
  current: number;
  pageSize: number;
}

// ── 中药远程搜索选择器 ─────────────────────────────────────────────────────────

interface HerbOption {
  label: string;
  value: string;
  herb_name: string;
}

interface HerbSelectProps {
  value?: string;
  onChange?: (value: string | undefined) => void;
  /** 选中药材后回传名称，由调用方写入 herb_name 字段。 */
  onPick?: (herbName: string) => void;
  /** 编辑回填时后端已有的药材名称，保证未搜索也能显示。 */
  initialLabel?: string;
}

/** 中药远程搜索 Select：不初始化加载全部，输入后 300ms 防抖请求 /herbs。 */
function HerbSelect({ value, onChange, onPick, initialLabel }: HerbSelectProps) {
  const [options, setOptions] = useState<HerbOption[]>([]);
  const [fetching, setFetching] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const optionsRef = useRef<HerbOption[]>([]);
  const valueRef = useRef<string | undefined>(value);
  valueRef.current = value;

  // 编辑回填：把已选药材以 herb_id+herb_name 注入选项
  useEffect(() => {
    if (value && initialLabel) {
      setOptions((prev) => {
        if (prev.some((o) => o.value === value)) return prev;
        const next = [
          { label: initialLabel, value, herb_name: initialLabel },
          ...prev,
        ];
        optionsRef.current = next;
        return next;
      });
    }
  }, [value, initialLabel]);

  // 卸载时清理防抖定时器
  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  const handleSearch = (keyword: string) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      setFetching(true);
      try {
        const resp = await fetchHerbs({ keyword: keyword || undefined, limit: 20 });
        const next: HerbOption[] = resp.items.map((h: Herb) => ({
          label: h.name,
          value: h.id,
          herb_name: h.name,
        }));
        // 保留当前已选项，避免搜索结果不含它时显示成 UUID
        const curVal = valueRef.current;
        if (curVal && !next.some((o) => o.value === curVal)) {
          const keep = optionsRef.current.find((o) => o.value === curVal);
          if (keep) next.unshift(keep);
        }
        optionsRef.current = next;
        setOptions(next);
      } catch {
        // 静默失败，保留已有选项
      } finally {
        setFetching(false);
      }
    }, 300);
  };

  return (
    <Select
      showSearch
      filterOption={false}
      onSearch={handleSearch}
      value={value}
      onChange={(val) => {
        onChange?.(val);
        if (!val) {
          onPick?.('');
          return;
        }
        const opt = optionsRef.current.find((o) => o.value === val);
        if (opt) onPick?.(opt.herb_name);
      }}
      options={options}
      loading={fetching}
      placeholder="输入中药名搜索"
      allowClear
      style={{ width: '100%' }}
      notFoundContent={fetching ? '搜索中…' : '输入关键词搜索中药'}
    />
  );
}

// ── 组成单行 ──────────────────────────────────────────────────────────────────

/** Form.List render 给出的 field 结构（结构化声明，避免深层类型导入）。 */
interface ListFieldLike {
  key: number | string;
  name: number;
}

interface IngredientRowProps {
  form: FormInstance<PrescriptionFormValues>;
  field: ListFieldLike;
  index: number;
  total: number;
  onMove: (from: number, to: number) => void;
  onRemove: (index: number) => void;
}

/** 方剂组成中的一行：中药 / 剂量 / 单位 / 炮制 / 角色 / 上移 / 下移 / 删除。 */
function IngredientRow({
  form,
  field,
  index,
  total,
  onMove,
  onRemove,
}: IngredientRowProps) {
  // 监听本行 herb_name，供中药 Select 未搜索时回显
  const herbName = Form.useWatch(
    ['ingredients', field.name, 'herb_name'],
    form,
  ) as string | undefined;

  const rowItemStyle = { marginBottom: 12 };

  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
      <div style={{ width: 220, flex: 'none' }}>
        <Form.Item
          name={[field.name, 'herb_id']}
          style={rowItemStyle}
          // preserve：行首次挂载遇 StrictMode 双挂载时，卸载不删除 store 中回填的值
          preserve
          rules={[
            { required: true, message: '请选择药材' },
            {
              validator: (_, value: string | undefined) => {
                if (!value) return Promise.resolve();
                const list = (form.getFieldValue('ingredients') ??
                  []) as IngredientFormItem[];
                const duplicated =
                  list.filter((it) => it?.herb_id === value).length > 1;
                return duplicated
                  ? Promise.reject(new Error('同一方剂不能重复选择该药材'))
                  : Promise.resolve();
              },
            },
          ]}
        >
          <HerbSelect
            initialLabel={herbName}
            onPick={(name) =>
              form.setFieldValue(
                ['ingredients', field.name, 'herb_name'],
                name,
              )
            }
          />
        </Form.Item>
      </div>
      <div style={{ width: 110, flex: 'none' }}>
        <Form.Item
          name={[field.name, 'amount']}
          style={rowItemStyle}
          preserve
        >
          <InputNumber
            min={0}
            precision={2}
            placeholder="剂量"
            style={{ width: '100%' }}
          />
        </Form.Item>
      </div>
      <div style={{ width: 90, flex: 'none' }}>
        <Form.Item
          name={[field.name, 'unit']}
          style={rowItemStyle}
          preserve
          rules={[{ max: 16, message: '最长 16 字' }]}
        >
          <Input placeholder="单位" />
        </Form.Item>
      </div>
      <div style={{ width: 130, flex: 'none' }}>
        <Form.Item
          name={[field.name, 'processing']}
          style={rowItemStyle}
          preserve
          rules={[{ max: 255, message: '最长 255 字' }]}
        >
          <Input placeholder="炮制" />
        </Form.Item>
      </div>
      <div style={{ width: 130, flex: 'none' }}>
        <Form.Item
          name={[field.name, 'role']}
          style={rowItemStyle}
          preserve
          rules={[{ max: 32, message: '最长 32 字' }]}
        >
          <Input placeholder="作用/角色" />
        </Form.Item>
      </div>
      <Space size={4} style={{ flex: 'none' }}>
        <Button
          icon={<ArrowUpOutlined />}
          disabled={index === 0}
          onClick={() => onMove(index, index - 1)}
          aria-label="上移"
        />
        <Button
          icon={<ArrowDownOutlined />}
          disabled={index === total - 1}
          onClick={() => onMove(index, index + 1)}
          aria-label="下移"
        />
        <Button
          danger
          icon={<DeleteOutlined />}
          onClick={() => onRemove(field.name)}
          aria-label="删除药材"
        />
      </Space>
      {/* 隐藏字段：保存 herb_name，仅用于编辑回填与名称回显 */}
      <Form.Item name={[field.name, 'herb_name']} hidden preserve>
        <Input />
      </Form.Item>
    </div>
  );
}

// ── 页面 ──────────────────────────────────────────────────────────────────────

export default function PrescriptionsPage() {
  const { message } = App.useApp();
  const user = useUserStore((s) => s.user);
  const isAdmin = user?.role === 'admin';
  const reqSeq = useRequestSeq(); // BUG-048：丢弃过期的列表响应

  // 列表数据
  const [prescriptions, setPrescriptions] = useState<Prescription[]>([]);
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
  const [detail, setDetail] = useState<Prescription | null>(null);

  // 新建/编辑 Modal
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState<ModalMode>('create');
  const [editTarget, setEditTarget] = useState<Prescription | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<PrescriptionFormValues>();
  // 批量选择（批量删除 → 回收站）
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);

  // ── 数据加载 ──────────────────────────────────────────────────────────────

  /** 加载列表；传入 opts 时使用显式条件，否则读取当前 state。 */
  const loadPrescriptions = async (opts?: QueryState) => {
    const kw = opts ? opts.keyword : keyword;
    const cat = opts ? opts.categoryId : categoryId;
    const tg = opts ? opts.tagId : tagId;
    const pg = opts ? opts.current : current;
    const ps = opts ? opts.pageSize : pageSize;
    // BUG-048：快速翻页/改条件时旧慢响应会覆盖新数据，用序号丢弃过期响应
    const reqId = reqSeq.begin();
    setLoading(true);
    try {
      const resp = await fetchPrescriptions({
        keyword: kw || undefined,
        category_id: cat,
        tag_id: tg,
        limit: ps,
        offset: (pg - 1) * ps,
      });
      if (!reqSeq.isLatest(reqId)) return;
      setPrescriptions(resp.items);
      setTotal(resp.total);
      // BUG-056：当前页已越界（删除/筛选后无数据）→ 回退一页重新加载
      if (resp.items.length === 0 && pg > 1) {
        setCurrent(pg - 1);
      }
    } catch (err) {
      if (reqSeq.isLatest(reqId)) message.error(pickErrorMessage(err, '方剂列表加载失败'));
    } finally {
      if (reqSeq.isLatest(reqId)) setLoading(false);
    }
  };

  // BUG-073（lint）：被下面挂载 effect 依赖，改为 useCallback 固定引用（行为不变）
  const loadCategories = useCallback(async () => {
    try {
      const tree = await fetchCategories({
        resource_type: 'prescription',
        tree: true,
      });
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
    void loadPrescriptions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current, pageSize]);

  useEffect(() => {
    void loadCategories();
    void loadTags();
  }, [loadCategories, loadTags]);

  // ── 查询操作 ──────────────────────────────────────────────────────────────

  const onSearch = () => {
    if (current === 1) {
      void loadPrescriptions({
        keyword,
        categoryId,
        tagId,
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
    setCategoryId(undefined);
    setTagId(undefined);
    if (current === 1) {
      void loadPrescriptions({
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
      efficacy: '',
      indications: '',
      usage_method: '',
      source: '',
      description: '',
      ingredients: [],
      tag_ids: [],
    });
    setModalOpen(true);
  };

  const openEdit = (prescription: Prescription) => {
    setModalMode('edit');
    setEditTarget(prescription);
    setModalOpen(true);
  };

  const handleModalOpenChange = (open: boolean) => {
    if (open && editTarget) {
      // 与 herbs/page.tsx 一致：等 Modal/Form mount 后再回填
      requestAnimationFrame(() => {
        form.setFieldsValue({
          name: editTarget.name,
          aliases: editTarget.aliases,
          category_id: editTarget.category_id,
          efficacy: editTarget.efficacy,
          indications: editTarget.indications,
          usage_method: editTarget.usage_method,
          source: editTarget.source,
          description: editTarget.description,
          // BUG-054：tags / ingredients 可能为 null（同 herbs 页已处理）
          tag_ids: (editTarget.tags ?? []).map((t) => t.id),
          // 后端结构 → Form.List 本地结构（去掉 id/sort_order，按序排列）
          ingredients: [...(editTarget.ingredients ?? [])]
            .sort((a, b) => a.sort_order - b.sort_order)
            .map((i) => ({
              herb_id: i.herb_id,
              herb_name: i.herb_name,
              amount: i.amount,
              unit: i.unit,
              processing: i.processing,
              role: i.role,
            })),
        });
      });
    }
  };

  const onSubmit = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);

      // Form.List 本地结构 → 后端提交结构：重新生成 sort_order，不提交 herb_name
      const ingredients = (values.ingredients ?? [])
        .filter((it) => it.herb_id)
        .map((it, idx) => ({
          herb_id: it.herb_id as string,
          amount: it.amount ?? null,
          unit: it.unit ?? '',
          processing: it.processing ?? '',
          role: it.role ?? '',
          sort_order: idx,
        }));

      const payload = {
        name: values.name.trim(),
        aliases: values.aliases ?? [],
        category_id: values.category_id ?? null,
        efficacy: values.efficacy ?? '',
        indications: values.indications ?? '',
        usage_method: values.usage_method ?? '',
        source: values.source ?? '',
        description: values.description ?? '',
        ingredients,
        tag_ids: values.tag_ids ?? [],
      };

      if (modalMode === 'edit' && editTarget) {
        await updatePrescription(editTarget.id, payload);
        message.success('方剂已更新');
      } else {
        await createPrescription(payload);
        message.success('方剂已创建');
      }

      setModalOpen(false);
      form.resetFields();
      setEditTarget(null);
      // BUG-056：新建记录落在第一页——当前不在第一页时必须回第一页，
      // 否则刷新后用户看不到刚创建的方剂（旧实现原地刷新当前页）。
      if (modalMode === 'create' && current !== 1) {
        setCurrent(1); // 由 effect 触发加载
      } else {
        await loadPrescriptions();
      }
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
      const res = await batchDeleteResources('prescription', selectedRowKeys as string[]);
      message.success(res.message ?? `已移入回收站 ${res.success ?? 0} 条`);
      if (res.failed?.length) {
        message.warning(`${res.failed.length} 条失败：${res.failed[0].reason}`);
      }
      setSelectedRowKeys([]);
      await loadPrescriptions();
    } catch (err) {
      message.error(pickErrorMessage(err, '批量删除失败'));
    }
  };

  const onDelete = async (prescription: Prescription) => {
    try {
      await deletePrescription(prescription.id);
      message.success(`方剂「${prescription.name}」已移入回收站`);
      // 若当前页只剩这一条且非首页，回退一页（由 effect 重新加载）
      if (prescriptions.length === 1 && current > 1) {
        setCurrent(current - 1);
      } else {
        await loadPrescriptions();
      }
    } catch (err) {
      message.error(pickErrorMessage(err, '删除失败'));
    }
  };

  // ── 列表列 ────────────────────────────────────────────────────────────────

  const columns: ColumnsType<Prescription> = [
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
      title: '分类',
      dataIndex: 'category',
      key: 'category',
      width: 120,
      render: (_: unknown, row: Prescription) => row.category?.name ?? '—',
    },
    {
      title: '功效',
      dataIndex: 'efficacy',
      key: 'efficacy',
      width: 190,
      ellipsis: true,
      render: (v: string) => v || '—',
    },
    {
      title: '主治',
      dataIndex: 'indications',
      key: 'indications',
      width: 200,
      ellipsis: true,
      render: (v: string) => v || '—',
    },
    {
      title: '用法',
      dataIndex: 'usage_method',
      key: 'usage_method',
      width: 160,
      ellipsis: true,
      render: (v: string) => v || '—',
    },
    {
      title: '来源',
      dataIndex: 'source',
      key: 'source',
      width: 150,
      ellipsis: true,
      render: (v: string) => v || '—',
    },
    {
      title: '组成摘要',
      key: 'ingredients',
      width: 210,
      render: (_: unknown, row: Prescription) => ingredientSummary(row),
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
      render: (_: unknown, row: Prescription) => (
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
                title={`确认删除方剂「${row.name}」？`}
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

  // ── 详情 Drawer 组成列 ─────────────────────────────────────────────────────

  const ingredientColumns: ColumnsType<PrescriptionIngredient> = [
    {
      title: '顺序',
      key: 'order',
      width: 64,
      render: (_: unknown, __: PrescriptionIngredient, idx: number) => idx + 1,
    },
    {
      title: '药材',
      dataIndex: 'herb_name',
      key: 'herb_name',
      render: (v: string) => v || '—',
    },
    {
      title: '剂量',
      dataIndex: 'amount',
      key: 'amount',
      width: 90,
      render: (v: number | null) =>
        v === null || v === undefined ? '—' : v,
    },
    {
      title: '单位',
      dataIndex: 'unit',
      key: 'unit',
      width: 80,
      render: (v: string) => v || '—',
    },
    {
      title: '炮制',
      dataIndex: 'processing',
      key: 'processing',
      width: 130,
      render: (v: string) => v || '—',
    },
    {
      title: '作用/角色',
      dataIndex: 'role',
      key: 'role',
      width: 130,
      render: (v: string) => v || '—',
    },
  ];

  // ── Header ────────────────────────────────────────────────────────────────

  const headerLeft = (
    <Space size="middle" align="center">
      <ProfileOutlined style={{ fontSize: 20 }} />
      <h2 style={{ margin: 0 }}>方剂管理</h2>
    </Space>
  );
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} onClick={() => void loadPrescriptions()}>
        刷新
      </Button>
      {isAdmin && (
        <Popconfirm
          title={`确认删除选中的 ${selectedRowKeys.length} 条方剂？`}
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
      <ResourceRecycleBin
        resourceType="prescription"
        label="方剂"
        onChanged={() => void loadPrescriptions()}
      />
      {isAdmin && (
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新建方剂
        </Button>
      )}
      <AdminHeaderRight />
    </Space>
  );

  // ── 标签 Select 选项（筛选与表单共用）──────────────────────────────────────

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

  const formInitial: PrescriptionFormValues = {
    name: '',
    aliases: [],
    category_id: null,
    efficacy: '',
    indications: '',
    usage_method: '',
    source: '',
    description: '',
    ingredients: [],
    tag_ids: [],
  };

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      {/* 查询区 */}
      <Card style={{ marginBottom: 16 }}>
        <Space size="middle" wrap>
          <Input.Search
            placeholder="按名称/别名/功效/主治等关键词搜索"
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
            treeData={[
              { title: '未填写分类', value: UNCATEGORIZED_VALUE },
              ...toTreeSelectData(categoryTree),
            ]}
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
      <Table<Prescription>
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={prescriptions}
        loading={loading}
        rowSelection={
          isAdmin
            ? { selectedRowKeys, onChange: (keys) => setSelectedRowKeys(keys) }
            : undefined
        }
        scroll={{ x: 1750 }}
        locale={{ emptyText: '暂无方剂数据' }}
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
        title="方剂详情"
        open={detail !== null}
        onClose={() => setDetail(null)}
        width={640}
        destroyOnClose
      >
        {detail && (
          <>
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
              <Descriptions.Item label="功效">
                {detail.efficacy || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="主治">
                {detail.indications || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="用法">
                {detail.usage_method || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="来源">
                {detail.source || '—'}
              </Descriptions.Item>
              <Descriptions.Item label="描述">
                {detail.description || '—'}
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

            <Typography.Title level={5} style={{ marginTop: 20 }}>
              方剂组成
            </Typography.Title>
            <Table<PrescriptionIngredient>
              rowKey="id"
              size="small"
              columns={ingredientColumns}
              dataSource={[...(detail.ingredients ?? [])].sort(
                (a, b) => a.sort_order - b.sort_order,
              )}
              pagination={false}
              locale={{ emptyText: '该方剂暂无组成药材' }}
            />
          </>
        )}
      </Drawer>

      {/* 新建/编辑 Modal */}
      <Modal
        title={modalMode === 'edit' ? '编辑方剂' : '新建方剂'}
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
        width={920}
      >
        <Form<PrescriptionFormValues>
          form={form}
          layout="vertical"
          initialValues={formInitial}
          preserve={false}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入方剂名称' },
              { whitespace: true, message: '名称不能为纯空格' },
              { max: 128, message: '最长 128 字' },
            ]}
          >
            <Input placeholder="如：麻黄汤" autoFocus />
          </Form.Item>

          <Form.Item name="aliases" label="别名（可回车添加多个）">
            <Select
              mode="tags"
              placeholder="如：麻黄散"
              tokenSeparators={[',', '，']}
            />
          </Form.Item>

          <Form.Item name="category_id" label="分类">
            <TreeSelect
              treeData={toTreeSelectData(categoryTree)}
              allowClear
              placeholder="选择方剂分类"
              treeDefaultExpandAll
            />
          </Form.Item>

          <Form.Item
            name="efficacy"
            label="功效"
            rules={[{ max: 5000, message: '最长 5000 字' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 4 }}
              placeholder="如：发汗解表，宣肺平喘"
            />
          </Form.Item>

          <Form.Item
            name="indications"
            label="主治"
            rules={[{ max: 5000, message: '最长 5000 字' }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 4 }}
              placeholder="主治病证描述"
            />
          </Form.Item>

          <Form.Item
            name="usage_method"
            label="用法"
            rules={[{ max: 255, message: '最长 255 字' }]}
          >
            <Input placeholder="如：水煎服，温服取微汗" />
          </Form.Item>

          <Form.Item
            name="source"
            label="来源"
            rules={[{ max: 255, message: '最长 255 字' }]}
          >
            <Input placeholder="如：《伤寒论》" />
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

          <Form.Item label="方剂组成">
            <Form.List name="ingredients">
              {(fields, { add, remove, move }) => (
                <div>
                  {/* 列头 */}
                  <div
                    style={{
                      display: 'flex',
                      gap: 8,
                      marginBottom: 4,
                      color: '#999',
                      fontSize: 12,
                    }}
                  >
                    <div style={{ width: 220, flex: 'none' }}>中药</div>
                    <div style={{ width: 110, flex: 'none' }}>剂量</div>
                    <div style={{ width: 90, flex: 'none' }}>单位</div>
                    <div style={{ width: 130, flex: 'none' }}>炮制</div>
                    <div style={{ width: 130, flex: 'none' }}>
                      作用/角色
                    </div>
                    <div style={{ width: 108, flex: 'none' }}>操作</div>
                  </div>

                  {fields.map((field, index) => (
                    <IngredientRow
                      key={field.key}
                      form={form}
                      field={field}
                      index={index}
                      total={fields.length}
                      onMove={move}
                      onRemove={remove}
                    />
                  ))}

                  <Button
                    type="dashed"
                    block
                    icon={<PlusOutlined />}
                    onClick={() =>
                      add({
                        herb_id: undefined,
                        herb_name: '',
                        amount: null,
                        unit: '',
                        processing: '',
                        role: '',
                      })
                    }
                  >
                    添加药材
                  </Button>
                </div>
              )}
            </Form.List>
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
