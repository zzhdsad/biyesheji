/**
 * BUG-059：列表"窗口渲染"工具（纯函数，不依赖 React / DOM）。
 *
 * 背景：一次性渲染上千张卡片会造成主线程长阻塞（dev 库实测 ~2900 个知识库时
 * 页面无响应）。修复思路是**只在前端限制渲染窗口**：首屏渲染 KB_WINDOW_SIZE 张，
 * 用户点"加载更多"再扩窗；后端 GET /kb 接口与其他消费方（侧边栏、资源挂载下拉、
 * 文档页筛选等）保持不变。
 *
 * 这里把边界计算抽成纯函数，保证"越界 / 数据变少"等 corner case 可以不渲染 DOM
 * 就验证，避免把状态逻辑锁在组件里无法测试。
 */

/** 首屏渲染数量（也是每次"加载更多"的扩窗步长）。 */
export const KB_WINDOW_SIZE = 24;

/**
 * 把"期望可见数量"收敛到合法区间 [0, total]。
 *
 * - 负数 / 非数字：按 0 处理（渲染空列表，而不是抛异常）
 * - 小数：向下取整（避免 slice 出现非整数行为差异）
 * - 超过总数：收敛为 total（数据被删除后不会残留 stale 窗口值）
 */
export function clampVisibleCount(total: number, visible: number): number {
  const safeTotal = Number.isFinite(total) && total > 0 ? Math.floor(total) : 0;
  if (Number.isNaN(visible) || visible <= 0) return 0;
  // Infinity 视为"全部可见"，收敛为 total（Math.min 已负责封顶）
  return Math.min(Math.floor(visible), safeTotal);
}

/**
 * 计算下一次"加载更多"后的可见数量（结果同样收敛到 [0, total]）。
 */
export function nextVisibleCount(
  total: number,
  visible: number,
  step: number = KB_WINDOW_SIZE,
): number {
  const safeStep = Number.isFinite(step) && step > 0 ? Math.floor(step) : 0;
  return clampVisibleCount(total, visible + safeStep);
}
