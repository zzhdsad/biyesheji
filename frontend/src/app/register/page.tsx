'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Alert, App, Button, Card, Form, Input, Typography } from 'antd';
import { LockOutlined, MailOutlined, UserOutlined } from '@ant-design/icons';
import { useUserStore } from '@/stores/userStore';

interface RegisterForm {
  email: string;
  username: string;
  password: string;
  confirm: string;
}

export default function RegisterPage() {
  const router = useRouter();
  const { register, loading, error, token } = useUserStore();
  const { message } = App.useApp();
  const [succeed, setSucceed] = useState(false);

  // 已登录 → 直接跳聊天页
  useEffect(() => {
    if (token) router.replace('/chat');
  }, [token, router]);

  const onFinish = async (values: RegisterForm) => {
    const ok = await register(values.email, values.username, values.password);
    if (ok) {
      setSucceed(true);
      message.success('注册成功，请登录');
      setTimeout(() => router.replace('/login'), 800);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f5f7fa',
        padding: 24,
      }}
    >
      <Card style={{ width: 420, borderRadius: 8 }} variant="borderless">
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <Typography.Title level={3} style={{ marginBottom: 4 }}>
            创建账号
          </Typography.Title>
          <Typography.Text type="secondary">注册后即可登录使用</Typography.Text>
        </div>

        {error && (
          <Alert
            type="error"
            message={error}
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}
        {succeed && (
          <Alert
            type="success"
            message="注册成功，即将跳转登录页"
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}

        <Form<RegisterForm> layout="vertical" onFinish={onFinish} autoComplete="off">
          <Form.Item
            label="邮箱"
            name="email"
            rules={[
              { required: true, message: '请输入邮箱' },
              { type: 'email', message: '邮箱格式不正确' },
            ]}
          >
            <Input prefix={<MailOutlined />} placeholder="邮箱" size="large" />
          </Form.Item>

          <Form.Item
            label="用户名"
            name="username"
            rules={[
              { required: true, message: '请输入用户名' },
              { min: 3, max: 64, message: '用户名长度 3-64 个字符' },
              {
                pattern: /^[A-Za-z0-9_-]+$/,
                message: '仅支持字母、数字、下划线、短横线',
              },
            ]}
          >
            <Input prefix={<UserOutlined />} placeholder="用户名" size="large" />
          </Form.Item>

          <Form.Item
            label="密码"
            name="password"
            rules={[
              { required: true, message: '请输入密码' },
              { min: 6, max: 128, message: '密码至少 6 个字符' },
            ]}
          >
            <Input.Password prefix={<LockOutlined />} placeholder="密码" size="large" />
          </Form.Item>

          <Form.Item
            label="确认密码"
            name="confirm"
            dependencies={['password']}
            rules={[
              { required: true, message: '请再次输入密码' },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue('password') === value) {
                    return Promise.resolve();
                  }
                  return Promise.reject(new Error('两次输入的密码不一致'));
                },
              }),
            ]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="确认密码"
              size="large"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 12 }}>
            <Button
              type="primary"
              htmlType="submit"
              size="large"
              block
              loading={loading}
              disabled={succeed}
            >
              注册
            </Button>
          </Form.Item>
        </Form>

        <div style={{ textAlign: 'center' }}>
          <Typography.Text type="secondary">
            已有账号？{' '}
            <Typography.Link href="/login">去登录 →</Typography.Link>
          </Typography.Text>
        </div>
      </Card>
    </div>
  );
}
