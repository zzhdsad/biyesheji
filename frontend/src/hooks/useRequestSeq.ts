'use client';

import { useRef } from 'react';
import { createRequestSeq, type RequestSeq } from '@/utils/requestSeq';

/**
 * 组件内使用的请求序号守卫（BUG-048）：见 utils/requestSeq.ts。
 *
 * 用法：
 * ```ts
 * const reqSeq = useRequestSeq();
 * const load = async () => {
 *   const id = reqSeq.begin();
 *   try {
 *     const resp = await fetchXxx();
 *     if (!reqSeq.isLatest(id)) return; // 过期响应丢弃
 *     setData(resp.items);
 *   } finally {
 *     if (reqSeq.isLatest(id)) setLoading(false);
 *   }
 * };
 * ```
 */
export function useRequestSeq(): RequestSeq {
  const ref = useRef<RequestSeq | null>(null);
  if (ref.current === null) ref.current = createRequestSeq();
  return ref.current;
}
