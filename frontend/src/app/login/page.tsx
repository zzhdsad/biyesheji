'use client';

import { useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Alert, Button, Card, Form, Input, Typography } from 'antd';
import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { useUserStore } from '@/stores/userStore';

interface LoginForm {
  username_or_email: string;
  password: string;
}

export default function LoginPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { login, loading, error, token } = useUserStore();

  // 已登录 → 直接跳目标页（避免登录页反复可访问）
  useEffect(() => {
    if (token) {
      const redirect = searchParams.get('redirect') || '/chat';
      router.replace(redirect);
    }
  }, [token, router, searchParams]);

  const onFinish = async (values: LoginForm) => {
    const ok = await login(values.username_or_email, values.password);
    if (ok) {
      const redirect = searchParams.get('redirect') || '/chat';
      router.replace(redirect);
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
      <Card style={{ width: 400, borderRadius: 8 }} variant="borderless">
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <Typography.Title level={3} style={{ marginBottom: 4 }}>
            智能知识问答平台
          </Typography.Title>
          <Typography.Text type="secondary">登录以开始使用</Typography.Text>
        </div>

        {error && (
          <Alert
            type="error"
            message={error}
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}

        <Form<LoginForm> layout="vertical" onFinish={onFinish} autoComplete="off">
          <Form.Item
            label="用户名 / 邮箱"
            name="username_or_email"
            rules={[{ required: true, message: '请输入用户名或邮箱' }]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder="用户名或邮箱"
              size="large"
            />
          </Form.Item>

          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              prefix={<LockOutlined />}
              placeholder="密码"
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
            >
              登录
            </Button>
          </Form.Item>
        </Form>

        <div style={{ textAlign: 'center' }}>
          <Typography.Text type="secondary">
            还没账号？{' '}
            <Typography.Link href="/register">去注册 →</Typography.Link>
          </Typography.Text>
        </div>
      </Card>
    </div>
  );
}
