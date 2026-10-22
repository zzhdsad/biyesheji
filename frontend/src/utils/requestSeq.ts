/**
 * 请求序号守卫（BUG-048 / BUG-013）。
 *
 * 列表页与会话切换都是"快速连续发起请求"的场景：用户翻页 / 改筛选条件 /
 * 切换会话后，先发出的慢响应后到，会覆盖后发出的新响应（数据与 total 不一致，
 * 或把已删会话的历史消息复活）。本工具用单调递增的序号标记每一次请求：
 * 只有"当前最新一次请求"的响应允许写入状态，其余一律丢弃。
 *
 * 设计为纯函数工厂（不依赖 React），便于 store 与单元测试直接使用；
 * 组件内用 hooks/useRequestSeq 包一层 useRef 保持引用稳定。
 */
export interface RequestSeq {
  /** 发起一次新请求：返回本次序号，此前发起的请求随即作废。 */
  begin(): number;
  /** 该序号是否仍是"最新一次请求"（false = 响应已过期，应丢弃）。 */
  isLatest(id: number): boolean;
  /** 作废所有在飞请求（切换视图 / 删除资源等场景）。 */
  invalidate(): void;
  /** 当前序号（用于"发起时快照"的场景，如流式回调校验归属）。 */
  current(): number;
}

export function createRequestSeq(): RequestSeq {
  let seq = 0;
  return {
    begin() {
      seq += 1;
      return seq;
    },
    isLatest(id: number) {
      return id === seq;
    },
    invalidate() {
      seq += 1;
    },
    current() {
      return seq;
    },
  };
}
