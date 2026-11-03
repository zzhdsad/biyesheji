'use client';

import { useCallback, useEffect, useRef } from 'react';

import { fetchImportProgress, type ImportProgressSnapshot } from '@/services/api';

/** 连续多少次 idle 才判定任务不存在（提交刚发出时进度可能还没写入）。 */
const IDLE_GIVE_UP = 5;

interface Options {
  sessionId: string;
  active: boolean;
  /** 每次拿到快照都会回调，用于实时更新进度条。 */
  onSnapshot?: (snapshot: ImportProgressSnapshot) => void;
  onDone: (snapshot: ImportProgressSnapshot) => void;
  onFailed: (snapshot: ImportProgressSnapshot) => void;
  intervalMs?: number;
}

/**
 * 导入进度轮询。
 *
 * 提交请求本身是长请求，页面卸载后 Promise 会丢失；因此只要 store 里
 * 还记着 `committing=true`，重新挂载时依靠 session_id 轮询恢复进度和结果。
 * 轮询失败只跳过本轮，不终止（瞬时网络抖动不应该让进度条卡死）。
 */
export function useImportProgressPolling({
  sessionId,
  active,
  onSnapshot,
  onDone,
  onFailed,
  intervalMs = 1000,
}: Options): void {
  // 用 ref 持有回调：调用方每次渲染都会传新函数，不能因此重启轮询
  const handlersRef = useRef({ onSnapshot, onDone, onFailed });
  handlersRef.current = { onSnapshot, onDone, onFailed };

  const start = useCallback(
    (isCancelled: () => boolean) => {
      let timer: number | undefined;
      let idleStreak = 0;

      const tick = async () => {
        try {
          const snapshot = await fetchImportProgress(sessionId);
          if (isCancelled()) return;
          handlersRef.current.onSnapshot?.(snapshot);
          if (snapshot.done) {
            handlersRef.current.onDone(snapshot);
            return;
          }
          if (snapshot.failed) {
            handlersRef.current.onFailed(snapshot);
            return;
          }
          idleStreak = snapshot.phase === 'idle' ? idleStreak + 1 : 0;
          if (idleStreak >= IDLE_GIVE_UP) {
            handlersRef.current.onFailed({
              ...snapshot,
              message:
                snapshot.message || '未找到进行中的导入任务，可能已过期或服务已重启',
            });
            return;
          }
        } catch {
          // 网络/服务瞬时不可用：保持轮询，下一轮再试
        }
        if (!isCancelled()) {
          timer = window.setTimeout(tick, intervalMs);
        }
      };

      void tick();
      return () => {
        if (timer) window.clearTimeout(timer);
      };
    },
    [intervalMs, sessionId],
  );

  useEffect(() => {
    if (!active || !sessionId) return undefined;
    let cancelled = false;
    const stop = start(() => cancelled);
    return () => {
      cancelled = true;
      stop();
    };
  }, [active, sessionId, start]);
}
