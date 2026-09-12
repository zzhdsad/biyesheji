'use client';

import { useEffect, useState, type Key } from 'react';
import {
  Alert,
  App,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  Upload,
  message,
  type UploadProps,
} from 'antd';
import {
  DeleteOutlined,
  DeleteRowOutlined,
  DownloadOutlined,
  EditOutlined,
  KeyOutlined,
  PlusOutlined,
  ReloadOutlined,
  RollbackOutlined,
  StopOutlined,
  TeamOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { RcFile } from 'antd/es/upload';
import type { UserOut, UserRole } from '@/types';
import {
  batchDeleteUsers,
  batchImportUsers,
  batchRestoreUsers,
  createUser,
  deleteUser,
  disableUser,
  downloadUserTemplate,
  enableUser,
  fetchTrashUsers,
  fetchUsers,
  purgeUser,
  resetUserPassword,
  restoreUser,
  updateUser,
} from '@/services/api';
import { AppLayout } from '@/components/layout/AppLayout';
import { AdminHeaderRight } from '@/components/layout/AppSider';

const ROLE_LABELS: Record<UserRole, string> = {
  admin: '管理员',
  member: '成员',
  viewer: '只读',
};
const ROLE_COLORS: Record<UserRole, string> = {
  admin: 'red',
  member: 'blue',
  viewer: 'default',
};

interface UserFormValues {
  username: string;
  email: string;
  name?: string;
  department?: string;
  role: UserRole;
}

type TabKey = 'active' | 'trash';

export default function UsersPage() {
  const { message } = App.useApp();

  const [tab, setTab] = useState<TabKey>('active');
  const [users, setUsers] = useState<UserOut[]>([]);
  const [trashUsers, setTrashUsers] = useState<UserOut[]>([]);
  const [loading, setLoading] = useState(false);

  // 创建弹窗
  const [createForm] = Form.useForm<UserFormValues>();
  const [createOpen, setCreateOpen] = useState(false);
  const [createSubmitting, setCreateSubmitting] = useState(false);
  const [createdPwd, setCreatedPwd] = useState<string | null>(null);

  // 编辑弹窗
  const [editForm] = Form.useForm<Partial<UserFormValues>>();
  const [editTarget, setEditTarget] = useState<UserOut | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);

  // 重置密码
  const [resetTarget, setResetTarget] = useState<UserOut | null>(null);
  const [resetLoading, setResetLoading] = useState(false);
  const [resetResult, setResetResult] = useState<string | null>(null);

  // 批量导入
  const [importOpen, setImportOpen] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState<{
    total: number;
    success: number;
    failed: number;
    errors: string[];
  } | null>(null);

  // 批量删除（活跃用户 tab）
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [batchDeleting, setBatchDeleting] = useState(false);
  const [batchDeleteErrors, setBatchDeleteErrors] = useState<string[] | null>(null);

  // 批量恢复（回收站 tab）
  const [trashSelectedRowKeys, setTrashSelectedRowKeys] = useState<Key[]>([]);
  const [batchRestoring, setBatchRestoring] = useState(false);
  const [batchRestoreErrors, setBatchRestoreErrors] = useState<string[] | null>(null);

  // ── 编辑表单初始化：等 Modal 打开后再 setFieldsValue ──
  const handleEditOpenChange = (open: boolean) => {
    if (open && editTarget) {
      // 等 Modal 动画完成、Form.Item 全部 mount 后再赋值
      requestAnimationFrame(() => {
        editForm.setFieldsValue({
          name: editTarget.name,
          department: editTarget.department,
          role: editTarget.role,
        });
      });
    }
  };

  const loadActive = async () => {
    setLoading(true);
    try {
      setUsers(await fetchUsers());
    } catch {
      message.error('活跃用户列表加载失败');
    } finally {
      setLoading(false);
    }
  };
  const loadTrash = async () => {
    setLoading(true);
    try {
      setTrashUsers(await fetchTrashUsers());
    } catch {
      message.error('回收站加载失败');
    } finally {
      setLoading(false);
    }
  };
  const reload = async () => {
    if (tab === 'active') await loadActive();
    else await loadTrash();
  };

  useEffect(() => {
    void loadActive();
  }, []);

  // ── 创建 ──
  const onCreate = async () => {
    try {
      const values = await createForm.validateFields();
      setCreateSubmitting(true);
      const res = await createUser({
        username: values.username,
        email: values.email,
        name: values.name,
        department: values.department,
        role: values.role,
      });
      setCreatedPwd(res.initial_password);
      message.success(`用户「${res.user.username}」创建成功`);
      setCreateOpen(false);
      createForm.resetFields();
      await loadActive();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      if (detail) message.error(`创建失败：${detail}`);
    } finally {
      setCreateSubmitting(false);
    }
  };

  // ── 编辑 ──
  const openEdit = (user: UserOut) => {
    setEditTarget(user);
  };
  const onEdit = async () => {
    if (!editTarget) return;
    try {
      const values = await editForm.validateFields();
      setEditSubmitting(true);
      await updateUser(editTarget.id, {
        name: values.name,
        department: values.department,
        role: values.role,
      });
      message.success('用户信息已更新');
      setEditTarget(null);
      editForm.resetFields();
      await loadActive();
    } catch {
      // 表单校验失败
    } finally {
      setEditSubmitting(false);
    }
  };

  // ── 单个删除（软删除） ──
  const onDelete = async (user: UserOut) => {
    try {
      await deleteUser(user.id);
      message.success(`用户「${user.username}」已移入回收站`);
      await loadActive();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail ?? '删除失败，请稍后再试');
    }
  };

  const onToggleActive = async (user: UserOut, enable: boolean) => {
    try {
      if (enable) {
        await enableUser(user.id);
        message.success(`用户「${user.username}」已启用`);
      } else {
        await disableUser(user.id);
        message.success(`用户「${user.username}」已禁用（离职处理）`);
      }
      await loadActive();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail ?? '操作失败，请稍后再试');
    }
  };

  // ── 重置密码 ──
  const openReset = async (user: UserOut) => {
    setResetTarget(user);
    setResetResult(null);
    setResetLoading(true);
    try {
      const res = await resetUserPassword(user.id);
      setResetResult(res.new_password);
      message.success(`用户「${res.username}」密码已重置`);
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail ?? '重置失败，请稍后再试');
      setResetTarget(null);
    } finally {
      setResetLoading(false);
    }
  };

  // ── 批量删除（预检通过才统一执行） ──
  const onBatchDelete = async () => {
    setBatchDeleting(true);
    setBatchDeleteErrors(null);
    try {
      const ids = selectedRowKeys.map(String);
      const res = await batchDeleteUsers(ids);
      message.success(`${res.success} 个用户已移入回收站`);
      setSelectedRowKeys([]);
      await loadActive();
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string | { message?: string; errors?: string[] } } } };
      const detail = err?.response?.data?.detail;
      // 预检失败：后端返回 { message, errors }
      if (typeof detail === 'object' && detail?.errors) {
        setBatchDeleteErrors(detail.errors);
        message.warning(detail.message ?? '以下用户无法删除，未执行任何操作');
      } else if (typeof detail === 'string') {
        message.error(`批量删除失败：${detail}`);
      } else {
        message.error('批量删除失败，请稍后再试');
      }
    } finally {
      setBatchDeleting(false);
    }
  };

  // ── 批量恢复（预检通过才统一执行） ──
  const onBatchRestore = async () => {
    setBatchRestoring(true);
    setBatchRestoreErrors(null);
    try {
      const ids = trashSelectedRowKeys.map(String);
      const res = await batchRestoreUsers(ids);
      message.success(`${res.success} 个用户已恢复`);
      setTrashSelectedRowKeys([]);
      await loadTrash();
    } catch (e) {
      const err = e as { response?: { data?: { detail?: string | { message?: string; errors?: string[] } } } };
      const detail = err?.response?.data?.detail;
      if (typeof detail === 'object' && detail?.errors) {
        setBatchRestoreErrors(detail.errors);
        message.warning(detail.message ?? '以下用户无法恢复，未执行任何操作');
      } else if (typeof detail === 'string') {
        message.error(`批量恢复失败：${detail}`);
      } else {
        message.error('批量恢复失败，请稍后再试');
      }
    } finally {
      setBatchRestoring(false);
    }
  };

  // ── 回收站：单个恢复 / 彻底删除 ──
  const onRestore = async (user: UserOut) => {
    try {
      await restoreUser(user.id);
      message.success(`用户「${user.username}」已恢复`);
      await loadTrash();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail ?? '恢复失败');
    }
  };
  const onPurge = async (user: UserOut) => {
    try {
      await purgeUser(user.id);
      message.success(`用户「${user.username}」已彻底删除`);
      await loadTrash();
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail ?? '彻底删除失败');
    }
  };

  // ── 导入 ──
  const handleImport: UploadProps['beforeUpload'] = async (file) => {
    setImporting(true);
    setImportResult(null);
    try {
      const res = await batchImportUsers(file as RcFile);
      setImportResult(res);
      if (res.success > 0) message.success(`成功导入 ${res.success} 个用户`);
      if (res.failed > 0) message.warning(`${res.failed} 条导入失败，请查看详情`);
      await loadActive();
    } catch (e) {
      const msg =
        (e as { response?: { data?: { message?: string } } })?.response?.data?.message ??
        '导入失败，请检查 CSV 格式';
      message.error(msg);
    } finally {
      setImporting(false);
    }
    return Upload.LIST_IGNORE;
  };

  // ── 表格列 ──
  const activeColumns: ColumnsType<UserOut> = [
    { title: '用户名', dataIndex: 'username', key: 'username', width: 140 },
    { title: '姓名', dataIndex: 'name', key: 'name', width: 120, render: (v: string) => v || '-' },
    { title: '部门', dataIndex: 'department', key: 'department', width: 120, render: (v: string) => v || '-' },
    { title: '邮箱', dataIndex: 'email', key: 'email', ellipsis: true },
    {
      title: '角色', dataIndex: 'role', key: 'role', width: 100,
      render: (r: UserRole) => <Tag color={ROLE_COLORS[r]}>{ROLE_LABELS[r] ?? r}</Tag>,
    },
    {
      title: '状态', dataIndex: 'is_active', key: 'is_active', width: 90,
      render: (v: boolean) => v ? <Tag color="success">启用</Tag> : <Tag color="volcano">禁用</Tag>,
    },
    {
      title: '首次改密', dataIndex: 'must_change_password', key: 'must_change_password', width: 100,
      render: (v: boolean) => v ? <Tag color="orange">待修改</Tag> : <Tag color="success">已设置</Tag>,
    },
    {
      title: '操作', key: 'action', width: 320,
      render: (_, row) => (
        <Space size={4} wrap>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(row)}>编辑</Button>
          <Popconfirm title="确认重置该用户的密码？" description="将生成新的随机密码，用户下次登录需重新修改。" okText="重置" cancelText="取消" onConfirm={() => void openReset(row)}>
            <Button size="small" icon={<KeyOutlined />}>重置密码</Button>
          </Popconfirm>
          {row.is_active === false ? (
            <Popconfirm title="确认启用该用户？" description="启用后用户可正常登录。" okText="启用" cancelText="取消" onConfirm={() => void onToggleActive(row, true)}>
              <Button size="small" type="primary" ghost icon={<ReloadOutlined />}>启用</Button>
            </Popconfirm>
          ) : (
            <Popconfirm title="确认禁用该用户（离职处理）？" description="禁用后用户无法登录，但数据保留，可随时启用恢复。" okText="禁用" okButtonProps={{ danger: true }} cancelText="取消" onConfirm={() => void onToggleActive(row, false)}>
              <Button size="small" danger icon={<StopOutlined />}>禁用</Button>
            </Popconfirm>
          )}
          <Popconfirm title="确认删除该用户？" description="用户将移入回收站，7 天内可恢复。" okText="删除" okButtonProps={{ danger: true }} cancelText="取消" onConfirm={() => onDelete(row)}>
            <Button size="small" danger icon={<DeleteOutlined />}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const trashColumns: ColumnsType<UserOut> = [
    { title: '用户名', dataIndex: 'username', key: 'username', width: 140 },
    { title: '姓名', dataIndex: 'name', key: 'name', width: 120, render: (v: string) => v || '-' },
    { title: '部门', dataIndex: 'department', key: 'department', width: 120, render: (v: string) => v || '-' },
    { title: '邮箱', dataIndex: 'email', key: 'email', ellipsis: true },
    {
      title: '角色', dataIndex: 'role', key: 'role', width: 100,
      render: (r: UserRole) => <Tag color={ROLE_COLORS[r]}>{ROLE_LABELS[r] ?? r}</Tag>,
    },
    {
      title: '删除时间', dataIndex: 'deleted_at', key: 'deleted_at', width: 170,
      render: (v: string) => v ? new Date(v).toLocaleString('zh-CN') : '-',
    },
    {
      title: '操作', key: 'action', width: 200,
      render: (_, row) => (
        <Space size={4}>
          <Popconfirm title="确认恢复该用户？" okText="恢复" cancelText="取消" onConfirm={() => onRestore(row)}>
            <Button size="small" icon={<RollbackOutlined />}>恢复</Button>
          </Popconfirm>
          <Popconfirm title="确认彻底删除？" description="删除后不可恢复！" okText="彻底删除" okButtonProps={{ danger: true }} cancelText="取消" onConfirm={() => onPurge(row)}>
            <Button size="small" danger icon={<DeleteOutlined />}>彻底删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  // ── Header ──
  const headerLeft = (
    <Space size="middle" align="center">
      <TeamOutlined style={{ fontSize: 20 }} />
      <h2 style={{ margin: 0 }}>用户管理</h2>
    </Space>
  );
  const headerRight = (
    <Space size="large" align="center">
      <Button icon={<ReloadOutlined />} onClick={() => void reload()}>刷新</Button>
      {tab === 'active' && (
        <>
          <Button icon={<DownloadOutlined />} onClick={() => void downloadUserTemplate()}>下载模板</Button>
          <Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>批量导入</Button>
          {selectedRowKeys.length > 0 && (
            <Popconfirm
              title={`确认批量删除选中的 ${selectedRowKeys.length} 个用户？`}
              description="以下用户将全部移入回收站，7 天内可恢复。只要有一个无法删除，就不会执行任何操作。"
              okText="批量删除"
              okButtonProps={{ danger: true }}
              cancelText="取消"
              onConfirm={() => void onBatchDelete()}
            >
              <Button danger icon={<DeleteRowOutlined />} loading={batchDeleting}>
                批量删除（{selectedRowKeys.length}）
              </Button>
            </Popconfirm>
          )}
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>添加用户</Button>
        </>
      )}
      <AdminHeaderRight />
    </Space>
  );

  // ── 渲染 ──
  return (
    <AppLayout pageTitle="" headerLeft={headerLeft} headerRight={headerRight}>
      <Tabs
        activeKey={tab}
        onChange={(k) => {
          setTab(k as TabKey);
          setSelectedRowKeys([]);
          setTrashSelectedRowKeys([]);
          if (k === 'trash') void loadTrash();
          else void loadActive();
        }}
        items={[
          {
            key: 'active',
            label: `活跃用户${users.length > 0 ? `（${users.length}）` : ''}`,
            children: (
              <Table
                rowKey="id"
                size="middle"
                columns={activeColumns}
                dataSource={users}
                loading={loading}
                pagination={{ pageSize: 10, showSizeChanger: true }}
                locale={{ emptyText: '暂无用户' }}
                rowSelection={{ selectedRowKeys, onChange: (keys) => setSelectedRowKeys(keys) }}
              />
            ),
          },
          {
            key: 'trash',
            label: `回收站${trashUsers.length > 0 ? `（${trashUsers.length}）` : ''}`,
            children: (
              <>
                <Alert
                  type="info"
                  showIcon
                  style={{ marginBottom: 16 }}
                  message="回收站说明"
                  description="删除用户后将在此保留 7 天，期间可恢复；超过 7 天将自动彻底删除。"
                />
                {trashSelectedRowKeys.length > 0 && (
                  <Space style={{ marginBottom: 12 }}>
                    <span style={{ color: '#666' }}>
                      已选中 {trashSelectedRowKeys.length} 个用户
                    </span>
                    <Popconfirm
                      title={`确认批量恢复选中的 ${trashSelectedRowKeys.length} 个用户？`}
                      description="只要有一个无法恢复（如邮箱/用户名已被占用），就不会执行任何操作。"
                      okText="批量恢复"
                      okButtonProps={{ type: 'primary' }}
                      cancelText="取消"
                      onConfirm={() => void onBatchRestore()}
                    >
                      <Button type="primary" icon={<RollbackOutlined />} loading={batchRestoring}>
                        批量恢复
                      </Button>
                    </Popconfirm>
                    <Button onClick={() => setTrashSelectedRowKeys([])}>清除选择</Button>
                  </Space>
                )}
                <Table
                  rowKey="id"
                  size="middle"
                  columns={trashColumns}
                  dataSource={trashUsers}
                  loading={loading}
                  pagination={{ pageSize: 10, showSizeChanger: true }}
                  locale={{ emptyText: '回收站为空' }}
                  rowSelection={{ selectedRowKeys: trashSelectedRowKeys, onChange: (keys) => setTrashSelectedRowKeys(keys) }}
                />
              </>
            ),
          },
        ]}
      />

      {/* 创建用户弹窗 */}
      <Modal
        title="添加用户"
        open={createOpen}
        onOk={onCreate}
        onCancel={() => { setCreateOpen(false); createForm.resetFields(); setCreatedPwd(null); }}
        okText="创建" cancelText="取消" confirmLoading={createSubmitting} destroyOnClose
      >
        {createdPwd && (
          <Alert
            type="success" showIcon style={{ marginBottom: 16 }}
            message="用户创建成功"
            description={
              <div>
                初始密码：<code style={{ fontSize: 16, fontWeight: 600 }}>{createdPwd}</code>
                <br />请将此密码通知员工，员工首次登录后需修改密码。
              </div>
            }
          />
        )}
        <Form<UserFormValues> form={createForm} layout="vertical" initialValues={{ role: 'member' }} preserve={false}>
          <Form.Item name="username" label="用户名" rules={[
            { required: true, message: '请输入用户名' },
            { min: 3, max: 64, message: '用户名长度 3-64 个字符' },
            { pattern: /^[A-Za-z0-9_-]+$/, message: '仅支持字母、数字、下划线、短横线' },
          ]}><Input placeholder="如：zhangsan" autoFocus /></Form.Item>
          <Form.Item name="email" label="邮箱" rules={[
            { required: true, message: '请输入邮箱' },
            { type: 'email', message: '邮箱格式不正确' },
          ]}><Input placeholder="如：zhangsan@company.com" /></Form.Item>
          <Form.Item name="name" label="姓名"><Input placeholder="如：张三" /></Form.Item>
          <Form.Item name="department" label="部门"><Input placeholder="如：技术部" /></Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select options={[
              { label: '管理员', value: 'admin' },
              { label: '成员', value: 'member' },
              { label: '只读', value: 'viewer' },
            ]} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 编辑用户弹窗 */}
      <Modal
        title="编辑用户"
        open={editTarget !== null}
        afterOpenChange={handleEditOpenChange}
        onOk={onEdit}
        onCancel={() => { setEditTarget(null); editForm.resetFields(); }}
        okText="保存" cancelText="取消" confirmLoading={editSubmitting} destroyOnClose
      >
        <Form<Partial<UserFormValues>> form={editForm} layout="vertical" preserve={false}>
          <Form.Item name="name" label="姓名"><Input placeholder="如：张三" /></Form.Item>
          <Form.Item name="department" label="部门"><Input placeholder="如：技术部" /></Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select options={[
              { label: '管理员', value: 'admin' },
              { label: '成员', value: 'member' },
              { label: '只读', value: 'viewer' },
            ]} />
          </Form.Item>
        </Form>
      </Modal>

      {/* 重置密码结果弹窗 */}
      <Modal
        title="重置密码成功"
        open={resetTarget !== null}
        onCancel={() => { setResetTarget(null); setResetResult(null); }}
        footer={[<Button key="close" type="primary" onClick={() => { setResetTarget(null); setResetResult(null); }}>我已知晓</Button>]}
        destroyOnClose
      >
        {resetTarget && (
          <Alert
            type="success" showIcon style={{ marginBottom: 16 }}
            message={`用户「${resetTarget.username}」的密码已重置`}
            description={
              <div>
                新密码：<code style={{ fontSize: 16, fontWeight: 600 }}>{resetLoading ? '生成中…' : resetResult}</code>
                <br />请将此密码通知员工，员工首次登录后需修改密码。
              </div>
            }
          />
        )}
      </Modal>

      {/* 批量导入弹窗 */}
      <Modal
        title="批量导入用户"
        open={importOpen}
        onCancel={() => { setImportOpen(false); setImportResult(null); }}
        footer={[<Button key="close" onClick={() => setImportOpen(false)}>关闭</Button>]}
        destroyOnClose
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 16 }}>
          请先下载 CSV 模板，按格式填写后上传。系统将为每位新用户生成随机初始密码，首次登录需修改密码。
        </Typography.Paragraph>
        <Space style={{ marginBottom: 16 }}>
          <Button icon={<DownloadOutlined />} onClick={() => void downloadUserTemplate()}>下载 CSV 模板</Button>
        </Space>
        <Upload accept=".csv" showUploadList={false} beforeUpload={handleImport}>
          <Button icon={<UploadOutlined />} loading={importing} type="primary">选择 CSV 文件上传</Button>
        </Upload>
        {importResult && (
          <Alert
            type={importResult.failed > 0 ? 'warning' : 'success'} showIcon style={{ marginTop: 16 }}
            message={`导入完成：共 ${importResult.total} 条，成功 ${importResult.success} 条，失败 ${importResult.failed} 条`}
            description={importResult.errors.length > 0 ? (
              <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
                {importResult.errors.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            ) : null}
          />
        )}
      </Modal>

      {/* 批量删除预检失败弹窗 */}
      <Modal
        title="无法批量删除"
        open={batchDeleteErrors !== null}
        onCancel={() => setBatchDeleteErrors(null)}
        footer={[<Button key="close" type="primary" onClick={() => setBatchDeleteErrors(null)}>我已知晓</Button>]}
        destroyOnClose
      >
        <Alert
          type="error" showIcon style={{ marginBottom: 16 }}
          message="以下用户无法删除，**未执行任何删除操作**"
          description={
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {batchDeleteErrors?.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          }
        />
      </Modal>

      {/* 批量恢复预检失败弹窗 */}
      <Modal
        title="无法批量恢复"
        open={batchRestoreErrors !== null}
        onCancel={() => setBatchRestoreErrors(null)}
        footer={[<Button key="close" type="primary" onClick={() => setBatchRestoreErrors(null)}>我已知晓</Button>]}
        destroyOnClose
      >
        <Alert
          type="error" showIcon style={{ marginBottom: 16 }}
          message="以下用户无法恢复，**未执行任何恢复操作**"
          description={
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {batchRestoreErrors?.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          }
        />
      </Modal>
    </AppLayout>
  );
}
