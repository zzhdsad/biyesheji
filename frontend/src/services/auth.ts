import { api } from './api';
import type { TokenResponse, UserOut } from '@/types';

/** 注册请求体（与后端 RegisterRequest 对应）。 */
export interface RegisterPayload {
  email: string;
  username: string;
  password: string;
}

/** 登录请求体（与后端 LoginRequest 对应）。 */
export interface LoginPayload {
  username_or_email: string;
  password: string;
}

/** 注册新用户（不自动登录，需调 apiLogin 拿 token）。 */
export async function apiRegister(payload: RegisterPayload): Promise<UserOut> {
  const { data } = await api.post<UserOut>('/auth/register', payload);
  return data;
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
