'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { ProcessingView } from '@/types';
import { describeApiError } from '@/utils/apiError';
import { shouldPollProcessing } from '@/utils/processingStatus';

/**
 * 处理概览轮询（第五阶段）：读一次，只有「处理中」才继续轮询，且有明确超时。
 *
 * 三条链路（文档 / 结构化 / 关联文件组）共用这一套读取与退出逻辑，
 * 因此不会出现某个抽屉一直转圈、或关掉页面还在后台空转的情况。
 * 本 Hook 只读概览接口，不触发任何解析或生成。
 */

/** 轮询间隔与最长等待：超时后停止自动刷新，由用户手动刷新或重试。 */
const POLL_INTERVAL_MS = 4_000;
const POLL_MAX_MS = 180_000;

/** 默认只在「处理中」轮询；文件抽屉会额外把「已派单待开始」也算进去。 */
const defaultIsRunning = (view: ProcessingView) => shouldPollProcessing(view);

export interface ProcessingOverviewState<T> {
  view: T | null;
  error: string | null;
  loading: boolean;
  /** 派发重试 / 保存内容后调用：立即重读一次，并在仍在跑时继续轮询。 */
  reload: () => void;
}

export function useProcessingOverview<T extends ProcessingView>(
  fetcher: () => Promise<T>,
  enabled: boolean,
  targetId: string | null | undefined,
  isRunning: (view: T) => boolean = defaultIsRunning,
): ProcessingOverviewState<T> {
  const [view, setView] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);
  // fetcher / isRunning 由调用方内联传入，放进 ref 才能保持 load 的依赖稳定
  const fetcherRef = useRef(fetcher);
  const runningRef = useRef(isRunning);
  useEffect(() => {
    fetcherRef.current = fetcher;
    runningRef.current = isRunning;
  });

  const load = useCallback(async (): Promise<T | null> => {
    setLoading(true);
    try {
      const data = await fetcherRef.current();
      setView(data);
      setError(null);
      return data;
    } catch (err) {
      setError(describeApiError(err));
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled || !targetId) {
      setView(null);
      setError(null);
      return;
    }
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const deadline = Date.now() + POLL_MAX_MS;
    const tick = async () => {
      const data = await load();
      if (disposed) return;
      // 只跟真正在跑的任务；到点仍未结束就停下，交给用户手动刷新，避免空转
      if (data && runningRef.current(data) && Date.now() < deadline) {
        timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      }
    };
    void tick();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [enabled, targetId, nonce, load]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { view, error, loading, reload };
}
