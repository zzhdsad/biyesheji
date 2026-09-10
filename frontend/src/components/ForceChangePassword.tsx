'use client';

import { useState } from 'react';
import { Alert, App, Form, Input, Modal } from 'antd';
import { LockOutlined } from '@ant-design/icons';
import { useUserStore } from '@/stores/userStore';

/**
 * 首次登录强制修改密码弹窗。
 *
 * 当用户 must_change_password 为 true 时，弹出不可关闭的模态框，
 * 用户必须修改密码后才能使用系统。
 */
export function ForceChangePassword() {
  const { mustChangePassword, changePassword } = useUserStore();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onOk = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);
      setError(null);
      const ok = await changePassword(values.old_password, values.new_password);
      if (ok) {
        message.success('密码修改成功，请继续使用');
        form.resetFields();
      } else {
        const err = useUserStore.getState().error;
        setError(err || '密码修改失败');
      }
    } catch {
      // 表单校验失败
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="首次登录：请修改密码"
      open={mustChangePassword}
      onOk={onOk}
      okText="确认修改"
      cancelText="退出登录"
      confirmLoading={submitting}
      maskClosable={false}
      closable={false}
      keyboard={false}
      onCancel={() => {
        // 取消即登出
        void useUserStore.getState().logout();
      }}
      destroyOnClose
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message="为保障账号安全，首次登录必须修改密码"
        description="请设置一个新密码，之后可用新密码登录系统。"
      />

      {error && (
        <Alert type="error" message={error} showIcon style={{ marginBottom: 16 }} />
      )}

      <Form form={form} layout="vertical">
        <Form.Item
          label="原密码"
          name="old_password"
          rules={[{ required: true, message: '请输入原密码' }]}
        >
          <Input.Password prefix={<LockOutlined />} placeholder="原密码" />
        </Form.Item>

        <Form.Item
          label="新密码"
          name="new_password"
          rules={[
            { required: true, message: '请输入新密码' },
            { min: 6, max: 128, message: '密码长度 6-128 个字符' },
          ]}
        >
          <Input.Password prefix={<LockOutlined />} placeholder="新密码（至少 6 位）" />
        </Form.Item>

        <Form.Item
          label="确认新密码"
          name="confirm_password"
          dependencies={['new_password']}
          rules={[
            { required: true, message: '请再次输入新密码' },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue('new_password') === value) {
                  return Promise.resolve();
                }
                return Promise.reject(new Error('两次输入的密码不一致'));
              },
            }),
          ]}
        >
          <Input.Password prefix={<LockOutlined />} placeholder="再次输入新密码" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
