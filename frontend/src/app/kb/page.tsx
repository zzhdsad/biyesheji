'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import {
  App,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Radio,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd';
import {
  AppstoreOutlined,
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import type { KnowledgeBase } from '@/types';
import { createKb, deleteKb, fetchKnowledgeBases, updateKb } from '@/services/api';
import { useChatStore } from '@/stores/chatStore';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';
import KbRecycleBin from '@/components/admin/KbRecycleBin';
// BUG-059：窗口渲染，避免一次性渲染上千张卡片阻塞主线程
import { KB_WINDOW_SIZE, clampVisibleCount, nextVisibleCount } from '@/utils/pagination';
import { createRequestSeq } from '@/utils/requestSeq';

interface KbFormValues {
  name: string;
  description?: string;
  visibility: 'public' | 'private';
}

export default function KBPage() {
  const { message } = App.useApp();

  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(false);
  // 当前渲染窗口大小；只影响本页渲染数量，不影响 GET /kb 请求参数
  const [visibleCount, setVisibleCount] = useState(KB_WINDOW_SIZE);
  // 搜索作用于**全量**列表：知识库多时新建的库可能被挤到窗口之外，
  // 只按前 24 个渲染会让用户误以为"知识库消失了"。
  const [query, setQuery] = useState('');
  // 列表请求序号：挂载时的慢响应不能覆盖创建后的新列表
  const loadSeq = useRef(createRequestSeq());

  // 创建弹窗
  const [createForm] = Form.useForm<KbFormValues>();
  const [createOpen, setCreateOpen] = useState(false);
  const [createSubmitting, setCreateSubmitting] = useState(false);

  // 编辑弹窗
  const [editForm] = Form.useForm<KbFormValues>();
  const [editTarget, setEditTarget] = useState<KnowledgeBase | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);

  const loadKbs = async () => {
    const reqId = loadSeq.current.begin();
    setLoading(true);
    try {
      const list = await fetchKnowledgeBases();
      if (!loadSeq.current.isLatest(reqId)) return;
      setKbs(list);
      // 重新加载后重置窗口（数据变化后旧窗口值可能越界）
      setVisibleCount(KB_WINDOW_SIZE);
    } catch {
      if (loadSeq.current.isLatest(reqId)) {
        message.error('知识库列表加载失败');
      }
    } finally {
      if (loadSeq.current.isLatest(reqId)) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    void loadKbs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 创建/编辑/删除后同步左侧栏（AppSider 数据源为 chatStore.knowledgeBases）
  const syncSider = () => {
    void useChatStore.getState().loadKnowledgeBases();
  };

  const onCreate = async () => {
    try {
      const values = await createForm.validateFields();
      setCreateSubmitting(true);
      const kb = await createKb(values);
      message.success(`知识库「${kb.name}」创建成功`);
      setCreateOpen(false);
      createForm.resetFields();
      await loadKbs();
      syncSider();
    } catch {
      // 表单校验失败，antd 自动提示
    } finally {
      setCreateSubmitting(false);
    }
  };

  const openEdit = (kb: KnowledgeBase) => {
    setEditTarget(kb);
    editForm.setFieldsValue({
      name: kb.name,
      description: kb.description,
      visibility: kb.visibility,
    });
  };

  const onEdit = async () => {
    if (!editTarget) return;
    try {
      const values = await editForm.validateFields();
      setEditSubmitting(true);
      const kb = await updateKb(editTarget.id, values);
      message.success(`知识库「${kb.name}」已更新`);
      setEditTarget(null);
      editForm.resetFields();
      await loadKbs();
      syncSider();
    } catch {
      // 表单校验失败
    } finally {
      setEditSubmitting(false);
    }
  };

  const onDelete = async (kb: KnowledgeBase) => {
    try {
      await deleteKb(kb.id);
      message.success(`知识库「${kb.name}」已删除`);
      await loadKbs();
      syncSider();
    } catch {
      message.error('删除失败，请稍后再试');
    }
  };

  const onSearchChange = (value: string) => {
    setQuery(value);
    setVisibleCount(KB_WINDOW_SIZE);
  };

  const keyword = query.trim().toLowerCase();
  const filteredKbs = keyword
    ? kbs.filter(
        (kb) =>
          kb.name.toLowerCase().includes(keyword) ||
          (kb.description ?? '').toLowerCase().includes(keyword),
      )
    : kbs;
  // 窗口内的卡片；clampVisibleCount 保证"数据变少"时不会残留越界窗口
  const renderedCount = clampVisibleCount(filteredKbs.length, visibleCount);
  const visibleKbs = filteredKbs.slice(0, renderedCount);
  const hasMore = filteredKbs.length > renderedCount;

  const headerLeft = <h2 style={{ margin: 0 }}>知识库管理</h2>;
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} onClick={() => void loadKbs()}>
        刷新
      </Button>
      {/* 批量删除 + 回收站（恢复 / 彻底删除 / 一键清空） */}
      <KbRecycleBin onChanged={() => void loadKbs()} />
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
        创建知识库
      </Button>
      <AdminHeaderRight />
    </Space>
  );

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
          marginBottom: 16,
        }}
      >
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索知识库名称或描述"
          value={query}
          onChange={(e) => onSearchChange(e.target.value)}
          style={{ maxWidth: 320 }}
        />
        <Typography.Text type="secondary">
          共 {kbs.length} 个知识库
          {keyword ? `，匹配 ${filteredKbs.length} 个` : `，当前显示 ${renderedCount} 个`}
        </Typography.Text>
      </div>
      <Spin spinning={loading}>
        {visibleKbs.length === 0 ? (
          <Empty description={keyword ? '没有匹配的知识库' : '暂无知识库'} />
        ) : (
          <Row gutter={[16, 16]}>
            {visibleKbs.map((kb) => (
              <Col key={kb.id} xs={24} sm={12} lg={8}>
                <Card
                  title={
                    <Typography.Text strong ellipsis style={{ maxWidth: '100%' }}>
                      {kb.name}
                    </Typography.Text>
                  }
                  extra={
                    <Tag color={kb.visibility === 'public' ? 'green' : 'gold'}>
                      {kb.visibility === 'public' ? '公开' : '私有'}
                    </Tag>
                  }
                  actions={[
                    <Button
                      key="edit"
                      type="text"
                      size="small"
                      icon={<EditOutlined />}
                      onClick={() => openEdit(kb)}
                    >
                      编辑
                    </Button>,
                    <Link
                      key="docs"
                      href={{ pathname: '/documents', query: { kb_id: kb.id } }}
                    >
                      <Button type="text" size="small" icon={<FileTextOutlined />}>
                        管理文档
                      </Button>
                    </Link>,
                    <Link
                      key="resources"
                      href={{ pathname: '/kb/resources', query: { kb_id: kb.id } }}
                    >
                      <Button type="text" size="small" icon={<AppstoreOutlined />}>
                        资源挂载
                      </Button>
                    </Link>,
                    <Popconfirm
                      key="delete"
                      title="确认删除该知识库？"
                      description="将移入回收站（保留期内可恢复）；在回收站中彻底删除才会清理文档、切片与向量。"
                      okText="删除"
                      okButtonProps={{ danger: true }}
                      cancelText="取消"
                      onConfirm={() => onDelete(kb)}
                    >
                      <Button type="text" size="small" danger icon={<DeleteOutlined />}>
                        删除
                      </Button>
                    </Popconfirm>,
                  ]}
                >
                  <Typography.Paragraph
                    type="secondary"
                    ellipsis={{ rows: 2 }}
                    style={{ marginBottom: 8 }}
                  >
                    {kb.description || '暂无描述'}
                  </Typography.Paragraph>
                  <Typography.Text type="secondary">
                    文档数：{kb.document_count ?? 0}
                  </Typography.Text>
                </Card>
              </Col>
            ))}
          </Row>
        )}
        {hasMore && (
          <div style={{ marginTop: 16, textAlign: 'center' }}>
            <Button onClick={() => setVisibleCount((cur) => nextVisibleCount(filteredKbs.length, cur))}>
              加载更多（已显示 {renderedCount} / 共 {filteredKbs.length}）
            </Button>
          </div>
        )}
      </Spin>

      {/* 创建知识库弹窗 */}
      <Modal
        title="创建知识库"
        open={createOpen}
        onOk={onCreate}
        onCancel={() => {
          setCreateOpen(false);
          createForm.resetFields();
        }}
        okText="创建"
        cancelText="取消"
        confirmLoading={createSubmitting}
        destroyOnClose
      >
        <Form<KbFormValues>
          form={createForm}
          layout="vertical"
          initialValues={{ visibility: 'private' }}
          preserve={false}
        >
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入知识库名称' },
              { max: 128, message: '名称最长 128 字' },
            ]}
          >
            <Input placeholder="如：员工手册" autoFocus />
          </Form.Item>
          <Form.Item name="description" label="描述（可选）">
            <Input.TextArea
              placeholder="简述该知识库的用途与范围"
              autoSize={{ minRows: 2, maxRows: 4 }}
              maxLength={2000}
              showCount
            />
          </Form.Item>
          <Form.Item name="visibility" label="可见性">
            <Radio.Group>
              <Radio value="private">私有（仅自己可见）</Radio>
              <Radio value="public">公开（所有人可读）</Radio>
            </Radio.Group>
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑知识库弹窗（partial update：未改字段保持原值） */}
      <Modal
        title="编辑知识库"
        open={editTarget !== null}
        onOk={onEdit}
        onCancel={() => {
          setEditTarget(null);
          editForm.resetFields();
        }}
        okText="保存"
        cancelText="取消"
        confirmLoading={editSubmitting}
        destroyOnClose
      >
        <Form<KbFormValues> form={editForm} layout="vertical" preserve={false}>
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入知识库名称' },
              { max: 128, message: '名称最长 128 字' },
            ]}
          >
            <Input placeholder="如：员工手册" />
          </Form.Item>
          <Form.Item name="description" label="描述（可选）">
            <Input.TextArea
              placeholder="简述该知识库的用途与范围"
              autoSize={{ minRows: 2, maxRows: 4 }}
              maxLength={2000}
              showCount
            />
          </Form.Item>
          <Form.Item name="visibility" label="可见性">
            <Radio.Group>
              <Radio value="private">私有（仅自己可见）</Radio>
              <Radio value="public">公开（所有人可读）</Radio>
            </Radio.Group>
          </Form.Item>
        </Form>
      </Modal>
    </AppLayout>
  );
}
