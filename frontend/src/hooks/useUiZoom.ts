'use client';

import { useCallback, useEffect, useState } from 'react';

const STORAGE_KEY = 'kp_ui_zoom';
const MIN_ZOOM = 70;
const MAX_ZOOM = 160;
const STEP = 10;

function clamp(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(value / STEP) * STEP));
}

function readStoredZoom(): number {
  if (typeof window === 'undefined') return 100;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const value = raw ? Number(raw) : 100;
    return Number.isFinite(value) ? clamp(value) : 100;
  } catch {
    return 100;
  }
}

/**
 * 全站 UI 缩放：
 * - 按钮控制 70% ~ 160%，10% 步进；
 * - Ctrl + 滚轮：上滚放大、下滚缩小；
 * - localStorage 持久化，默认 100%。
 */
export function useUiZoom() {
  const [zoom, setZoomState] = useState(100);

  useEffect(() => {
    setZoomState(readStoredZoom());
  }, []);

  const setZoom = useCallback((next: number | ((current: number) => number)) => {
    setZoomState((current) => {
      const value = clamp(typeof next === 'function' ? next(current) : next);
      try {
        window.localStorage.setItem(STORAGE_KEY, String(value));
      } catch {
        // localStorage 不可用时仅当前会话生效
      }
      return value;
    });
  }, []);

  const zoomIn = useCallback(() => setZoom((v) => v + STEP), [setZoom]);
  const zoomOut = useCallback(() => setZoom((v) => v - STEP), [setZoom]);
  const resetZoom = useCallback(() => setZoom(100), [setZoom]);

  useEffect(() => {
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      setZoom((current) => current + (event.deltaY < 0 ? STEP : -STEP));
    };
    window.addEventListener('wheel', onWheel, { passive: false });
    return () => window.removeEventListener('wheel', onWheel);
  }, [setZoom]);

  return { zoom, setZoom, zoomIn, zoomOut, resetZoom };
}
