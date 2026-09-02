import type { UserOut } from '@/types';

/**
 * Token / 用户信息的本地存储层。
 *
 * 设计动机：api.ts 的 axios 拦截器需要读 token，userStore 需要写 token，
 * 而 userStore → auth.ts → api.ts 会形成循环依赖。把存储读写抽到这个
 * 零依赖的独立模块，api.ts 与 userStore 都从这里读写，断开环。
 *
 * 双写策略（计划文档决策）：
 * - localStorage：axios 拦截器读 token 用
 * - cookie：Next.js middleware（SSR 侧路由守卫）读 token 用，因为 middleware 读不到 localStorage
 */

const TOKEN_KEY = 'kp_token';
const USER_KEY = 'kp_user';

/** localStorage 在 SSR 侧不可用，用模块级缓存兜底，避免每次读盘。 */
let cachedToken: string | null = null;

export function getToken(): string | null {
  if (cachedToken) return cachedToken;
  if (typeof window !== 'undefined') {
    cachedToken = window.localStorage.getItem(TOKEN_KEY);
  }
  return cachedToken;
}

export function setToken(token: string): void {
  cachedToken = token;
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(TOKEN_KEY, token);
    // 同步写 cookie，供 Next.js middleware SSR 守卫读取
    const maxAge = 60 * 60 * 24 * 7; // 7 天，与后端 ACCESS_TOKEN_EXPIRE_MINUTES 一致
    document.cookie = `${TOKEN_KEY}=${token}; path=/; max-age=${maxAge}; SameSite=Lax`;
  }
}

export function clearToken(): void {
  cachedToken = null;
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(USER_KEY);
    document.cookie = `${TOKEN_KEY}=; path=/; max-age=0; SameSite=Lax`;
  }
}

export function getStoredUser(): UserOut | null {
  if (typeof window === 'undefined') return null;
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as UserOut;
  } catch {
    return null;
  }
}

export function setStoredUser(user: UserOut): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(USER_KEY, JSON.stringify(user));
}

/** cookie 键名（Next.js middleware 读取用）。 */
export const TOKEN_COOKIE_KEY = TOKEN_KEY;
