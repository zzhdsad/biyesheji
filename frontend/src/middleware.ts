import { NextResponse, type NextRequest } from 'next/server';

/**
 * Next.js 路由守卫（SSR 侧，Edge Runtime）。
 *
 * middleware 读不到 localStorage，只能读 cookie；故 userStore 登录时把 token
 * 同步写入 cookie（见 services/token.ts），此处从 cookie 取 token 做页面级保护。
 *
 * 真正的 API 鉴权由后端 JWT 中间件 + axios 拦截器负责，middleware 只做页面层
 * "未登录 → 跳 /login" 的快速拦截，避免业务页 SSR 后才发现 401。
 */

const TOKEN_COOKIE_KEY = 'kp_token';

/** 公开页面（无需登录即可访问）。无开放注册，/register 已下线。 */
const PUBLIC_PAGES = ['/login'];

function isPublic(pathname: string): boolean {
  return PUBLIC_PAGES.some((p) => pathname === p || pathname.startsWith(p + '/'));
}

export function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  if (isPublic(pathname)) {
    return NextResponse.next();
  }

  const token = req.cookies.get(TOKEN_COOKIE_KEY)?.value;

  // 问题6：根路径角色明确定义——已登录访问 / 直接进入主界面 /chat
  // （未登录走下方统一拦截到 /login）
  if (pathname === '/' && token) {
    const chatUrl = req.nextUrl.clone();
    chatUrl.pathname = '/chat';
    chatUrl.search = '';
    return NextResponse.redirect(chatUrl);
  }

  if (token) {
    return NextResponse.next();
  }

  // 未登录 → 跳登录页并带上 redirect 参数，登录成功后回跳原页面
  const loginUrl = req.nextUrl.clone();
  loginUrl.pathname = '/login';
  loginUrl.search = `?redirect=${encodeURIComponent(pathname + search)}`;
  return NextResponse.redirect(loginUrl);
}

export const config = {
  // 排除：静态资源、Next 内部、API 代理、健康检查；只对页面做守卫
  matcher: ['/((?!_next/static|_next/image|favicon.ico|api|health).*)'],
};
