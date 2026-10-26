'use client';

import { Suspense, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Alert, Button, Card, Checkbox, Form, Input, theme, Typography } from 'antd';
import { LockOutlined, UserOutlined } from '@ant-design/icons';
import { useUserStore } from '@/stores/userStore';

const REMEMBER_LOGIN_KEY = 'kp_remember_login';

interface LoginFormValues {
  username_or_email: string;
  password: string;
}

/**
 * 登录表单内层组件。
 * 注意：useSearchParams() 在 Next.js 14 静态预渲染时要求 Suspense 边界
 * （missing-suspense-with-csr-bailout），因此默认导出用 <Suspense> 包裹。
 */
/**
 * 只允许站内绝对路径跳转，杜绝开放重定向（BUG-028）：
 * `?redirect=https://evil.com` 或 `//evil.com` 会被丢弃并回退到 /chat。
 */
function safeRedirect(raw: string | null | undefined): string {
  if (!raw) return '/chat';
  if (!raw.startsWith('/') || raw.startsWith('//') || raw.startsWith('/\\')) {
    return '/chat';
  }
  return raw;
}

function LoginForm() {
  const searchParams = useSearchParams();
  const { login, loading, error, token } = useUserStore();
  const { token: uiToken } = theme.useToken();
  const [form] = Form.useForm<LoginFormValues>();
  const [remember, setRemember] = useState(true);

  // 自动填充上次成功登录的账号/密码；仅保存在本机浏览器。
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(REMEMBER_LOGIN_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as Partial<LoginFormValues>;
      if (saved.username_or_email) {
        form.setFieldsValue({
          username_or_email: saved.username_or_email,
          password: saved.password ?? '',
        });
        setRemember(true);
      }
    } catch {
      // 存储损坏时忽略，不阻断登录
    }
  }, [form]);

  // 已登录 → 直接跳目标页（避免登录页反复可访问）
  useEffect(() => {
    if (token) {
      window.location.assign(safeRedirect(searchParams.get('redirect')));
    }
  }, [token, searchParams]);

  const onFinish = async (values: LoginFormValues) => {
    const ok = await login(values.username_or_email, values.password);
    if (ok) {
      try {
        if (remember) {
          window.localStorage.setItem(
            REMEMBER_LOGIN_KEY,
            JSON.stringify({
              username_or_email: values.username_or_email,
              password: values.password,
            }),
          );
        } else {
          window.localStorage.removeItem(REMEMBER_LOGIN_KEY);
        }
      } catch {
        // localStorage 不可用时静默跳过自动填充
      }
      // 问题5：登录成功后整页硬跳，而非 SPA 软导航。
      // 软导航曾出现"点击登录进不去、刷新后才进去"的竞态：跳转瞬间新页面的
      // 首个请求若赶在 token/cookie 完全生效前发出，401 会被拦截器弹回登录页。
      // 硬跳让浏览器以全新生命周期加载目标页：middleware 读 cookie、AuthInit
      // 读 localStorage 时 token 均已稳定落盘，点击一次即进入。
      window.location.assign(safeRedirect(searchParams.get('redirect')));
    }
  };

  return (
    <div
      className="login-page"
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
    >
      <Card
        className="login-card"
        style={{ width: 420, borderRadius: 16, border: `1px solid ${uiToken.colorBorder}` }}
        variant="borderless"
      >
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div className="login-mark">AI · TCM</div>
          <Typography.Title level={3} className="login-title" style={{ marginBottom: 4 }}>
            中医药知识助手
          </Typography.Title>
          <Typography.Text type="secondary">
            知识资源管理 · 可溯源智能问答
          </Typography.Text>
        </div>

        {error && (
          <Alert
            type="error"
            message={error}
            showIcon
            style={{ marginBottom: 16 }}
          />
        )}

        <Form<LoginFormValues>
          form={form}
          layout="vertical"
          onFinish={onFinish}
          autoComplete="on"
        >
          <Form.Item
            label="用户名 / 邮箱"
            name="username_or_email"
            rules={[{ required: true, message: '请输入用户名或邮箱' }]}
          >
            <Input
              prefix={<UserOutlined />}
              placeholder="用户名或邮箱"
              size="large"
              autoComplete="username"
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
              autoComplete="current-password"
            />
          </Form.Item>

          <Form.Item style={{ marginBottom: 8 }}>
            <Checkbox
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
            >
              记住账号和密码
            </Checkbox>
            <Typography.Text
              type="secondary"
              style={{ display: 'block', fontSize: 12, marginTop: 4 }}
            >
              仅保存在本机浏览器；公用电脑请取消勾选。
            </Typography.Text>
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
            没有账号？请联系管理员开通
          </Typography.Text>
        </div>
      </Card>
    </div>
  );
}

/** 默认导出：Suspense 边界包裹 useSearchParams（Next.js 14 静态预渲染要求）。 */
export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
