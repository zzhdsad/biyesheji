import { redirect } from 'next/navigation';

/**
 * 根路径 /（问题6：路由分离）。
 *
 * 角色定义：
 * - 未登录：middleware 拦截 → /login（独立登录页，无侧边栏/顶栏）
 * - 已登录：middleware 重定向 → /chat（主界面）
 *
 * 本组件是 middleware 之外的兜底（例如 matcher 未覆盖的极端情况），
 * 直接服务端重定向到 /chat。原工作台首页已按"登录网址与主界面分离"
 * 的决策下线，问答页 /chat 即系统主界面。
 */
export default function HomePage() {
  redirect('/chat');
}
