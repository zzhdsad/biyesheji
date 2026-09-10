import { create } from 'zustand';
import type { UserOut } from '@/types';
import { apiChangePassword, apiLogin, apiLogout, apiRegister, apiFetchMe } from '@/services/auth';
import {
  clearToken,
  getStoredUser,
  getToken,
  setStoredUser,
  setToken,
} from '@/services/token';

interface UserState {
  token: string | null;
  user: UserOut | null;
  loading: boolean; // 登录/注册请求进行中
  initializing: boolean; // 应用启动时恢复登录态进行中
  error: string | null;
  /** 是否强制修改密码（首次登录/管理员重置后） */
  mustChangePassword: boolean;

  /** 从 localStorage 恢复登录态（应用启动调用一次）。 */
  init: () => Promise<void>;
  /** 登录：成功写入 token + user，返回是否成功。 */
  login: (usernameOrEmail: string, password: string) => Promise<boolean>;
  /** 注册：不自动登录（与后端语义一致），返回是否成功。 */
  register: (email: string, username: string, password: string) => Promise<boolean>;
  /** 登出：清本地态 + 调后端语义端点 + 跳登录页。 */
  logout: () => Promise<void>;
  /** 修改密码（首次登录强制修改时调用）。 */
  changePassword: (oldPassword: string, newPassword: string) => Promise<boolean>;
  /** 是否已登录。 */
  isAuthenticated: () => boolean;
}

export const useUserStore = create<UserState>((set, get) => ({
  token: null,
  user: null,
  loading: false,
  initializing: true,
  error: null,
  mustChangePassword: false,

  init: async () => {
    const token = getToken();
    const user = getStoredUser();
    if (!token) {
      set({ initializing: false, token: null, user: null });
      return;
    }
    // 有本地 token → 先乐观恢复，再调 /me 校验有效性
    set({
      token,
      user,
      mustChangePassword: user?.must_change_password ?? false,
      initializing: false,
    });
    try {
      const fresh = await apiFetchMe();
      setStoredUser(fresh);
      set({ user: fresh, mustChangePassword: fresh.must_change_password ?? false });
    } catch {
      // /me 失败（token 过期/无效）→ 清态（响应拦截器已处理跳转，这里只清状态）
      clearToken();
      set({ token: null, user: null, mustChangePassword: false });
    }
  },

  login: async (usernameOrEmail, password) => {
    set({ loading: true, error: null });
    try {
      const resp = await apiLogin({
        username_or_email: usernameOrEmail,
        password,
      });
      setToken(resp.access_token);
      setStoredUser(resp.user);
      set({
        token: resp.access_token,
        user: resp.user,
        mustChangePassword: resp.user.must_change_password ?? false,
        loading: false,
      });
      return true;
    } catch (err) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        '登录失败，请稍后重试';
      set({ loading: false, error: detail });
      return false;
    }
  },

  register: async (email, username, password) => {
    set({ loading: true, error: null });
    try {
      await apiRegister({ email, username, password });
      set({ loading: false });
      return true;
    } catch (err) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        '注册失败，请稍后重试';
      set({ loading: false, error: detail });
      return false;
    }
  },

  logout: async () => {
    // 调后端语义端点（失败也不阻断本地登出）；token 失效时后端返回 401，忽略即可
    try {
      await apiLogout();
    } catch {
      // 忽略：本地清态即可
    }
    clearToken();
    set({ token: null, user: null, mustChangePassword: false });
    if (typeof window !== 'undefined') {
      window.location.href = '/login';
    }
  },

  changePassword: async (oldPassword, newPassword) => {
    try {
      await apiChangePassword(oldPassword, newPassword);
      // 更新本地用户状态
      const user = get().user;
      if (user) {
        const updated = { ...user, must_change_password: false };
        setStoredUser(updated);
        set({ user: updated, mustChangePassword: false });
      }
      return true;
    } catch (err) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        '密码修改失败';
      set({ error: detail });
      return false;
    }
  },

  isAuthenticated: () => !!get().token,
}));
