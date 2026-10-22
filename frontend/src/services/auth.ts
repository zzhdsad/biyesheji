import { api } from './api';
import type { TokenResponse, UserOut } from '@/types';

/**
 * 账号由管理员在「用户管理」中创建（POST /users），系统不提供开放注册，
 * 后端也没有 /auth/register 端点，因此这里不提供注册接口（BUG-014）。
 */

/** 登录请求体（与后端 LoginRequest 对应）。 */
export interface LoginPayload {
  username_or_email: string;
  password: string;
}

/** 用户名或邮箱登录，返回 JWT。 */
export async function apiLogin(payload: LoginPayload): Promise<TokenResponse> {
  const { data } = await api.post<TokenResponse>('/auth/login', payload);
  return data;
}

/** 登出（语义端点，前端删 token；无服务端黑名单）。 */
export async function apiLogout(): Promise<void> {
  await api.post('/auth/logout');
}

/** 获取当前登录用户信息。 */
export async function apiFetchMe(): Promise<UserOut> {
  const { data } = await api.get<UserOut>('/auth/me');
  return data;
}

/** 修改密码（首次登录强制修改时调用）。 */
export async function apiChangePassword(oldPassword: string, newPassword: string): Promise<void> {
  await api.post('/auth/change-password', { old_password: oldPassword, new_password: newPassword });
}
