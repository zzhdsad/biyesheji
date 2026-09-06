'use client';

import { useEffect, useState } from 'react';
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
  DeleteOutlined,
  EditOutlined,
  FileTextOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import type { KnowledgeBase } from '@/types';
import { createKb, deleteKb, fetchKnowledgeBases, updateKb } from '@/services/api';
import { useChatStore } from '@/stores/chatStore';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';

interface KbFormValues {
  name: string;
  description?: string;
  visibility: 'public' | 'private';
}

export default function KBPage() {
  const { message } = App.useApp();

  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(false);

  // 创建弹窗
  const [createForm] = Form.useForm<KbFormValues>();
  const [createOpen, setCreateOpen] = useState(false);
  const [createSubmitting, setCreateSubmitting] = useState(false);

  // 编辑弹窗
  const [editForm] = Form.useForm<KbFormValues>();
  const [editTarget, setEditTarget] = useState<KnowledgeBase | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);

  const loadKbs = async () => {
    setLoading(true);
    try {
      const list = await fetchKnowledgeBases();
      setKbs(list);
    } catch {
      message.error('知识库列表加载失败');
    } finally {
      setLoading(false);
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

  const headerLeft = <h2 style={{ margin: 0 }}>知识库管理</h2>;
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} onClick={() => void loadKbs()}>
        刷新
      </Button>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
        创建知识库
      </Button>
      <AdminHeaderRight />
    </Space>
  );

  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      <Spin spinning={loading}>
        {kbs.length === 0 ? (
          <Empty description="暂无知识库" />
        ) : (
          <Row gutter={[16, 16]}>
            {kbs.map((kb) => (
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
                    <Popconfirm
                      key="delete"
                      title="确认删除该知识库？"
                      description="将同时级联删除其下所有文档、切片与向量数据，不可恢复。"
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
