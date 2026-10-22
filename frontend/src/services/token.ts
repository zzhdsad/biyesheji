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

/**
 * BUG-061：cookie 有效期必须与后端 JWT 有效期保持一致。
 *
 * 后端 `ACCESS_TOKEN_EXPIRE_MINUTES = 1440`（即 24 小时，见 backend/src/core/config.py），
 * 此前前端写死 7 天：JWT 已过期但 cookie 仍在的 6 天里，Next.js middleware 会误判为
 * "已登录"并放行受保护路由，用户要等到首个接口返回 401 才被硬跳登录页。
 * 这里统一为 24 小时：**不**反向把 JWT TTL 改成 7 天（那是放宽安全配置）。
 *
 * 注意：后端若通过环境变量调整 JWT 有效期，此常量需同步修改；
 * `tokenMaxAge.test.ts` 会锁定"前端 cookie 有效期不得超过后端 JWT 有效期"这一不变量。
 */
export const TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24; // 24 小时 = 1440 分钟

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
    // 有效期取自 TOKEN_MAX_AGE_SECONDS（24 小时），与后端 JWT TTL 对齐（BUG-061）
    const maxAge = TOKEN_MAX_AGE_SECONDS;
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

/**
 * 401（未鉴权 / token 过期）的统一处理：清 token + 硬跳登录页（BUG-062）。
 *
 * 用 window.location 硬跳，避免 SPA 路由守卫与拦截器互相触发造成循环。
 * axios 拦截器（api.ts）与 SSE（hooks/useSSE.ts）共用此实现：SSE 走原生
 * fetch，不过 axios 拦截器，旧实现在 token 过期时只抛"请求失败"，既不清
 * token 也不跳转，用户会一直停在失效的页面上重试。
 */
export function handleUnauthorized(): void {
  if (typeof window === 'undefined') return;
  // /login 自身的 401 不跳转（避免登录页请求失败时反复跳转）
  const path = window.location.pathname;
  if (path.startsWith('/login')) return;
  clearToken();
  const redirect = encodeURIComponent(path + window.location.search);
  window.location.href = `/login?redirect=${redirect}`;
}

/** cookie 键名（Next.js middleware 读取用）。 */
export const TOKEN_COOKIE_KEY = TOKEN_KEY;
