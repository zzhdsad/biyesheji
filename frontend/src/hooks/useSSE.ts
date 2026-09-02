'use client';

import { useCallback } from 'react';
import type { Citation } from '@/types';
import { getToken } from '@/services/token';

/** SSE 事件回调集合（与后端 chat.py /ask-stream 事件协议对应）。 */
export interface SSEHandlers {
  onStart?: (data: { conversation_id: string }) => void;
  onCitations?: (data: { citations: Citation[] }) => void;
  onDelta?: (data: { content: string }) => void;
  onDone?: (data: { conversation_id: string; message_id: string }) => void;
  onError?: (data: { message: string }) => void;
}

/**
 * 用 fetch POST + ReadableStream 解析 SSE 文本流。
 *
 * EventSource 仅支持 GET，无法承载 POST body（question/kb_ids/conversation_id），
 * 故采用 fetch + 手动解析 SSE 行协议（event:/data:/空行分隔）。
 *
 * 后端每个事件为单行 data（JSON），仍按 SSE 规范支持多行 data 拼接。
 */
export async function streamSSE(
  url: string,
  body: unknown,
  handlers: SSEHandlers,
): Promise<void> {
  // 流式 fetch 不走 axios，需手动注入 Authorization（鉴权后 /chat/ask-stream 受保护）
  const token = getToken();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`SSE 请求失败：HTTP ${resp.status}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let currentEvent = '';
  let pendingData = '';

  /** 派发一个完整事件到对应回调。 */
  const dispatch = (event: string, dataStr: string): void => {
    if (!event || !dataStr) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(dataStr);
    } catch {
      return; // 忽略无法解析的 data
    }
    const data = parsed as Record<string, unknown>;
    switch (event) {
      case 'start':
        handlers.onStart?.(data as unknown as { conversation_id: string });
        break;
      case 'citations':
        handlers.onCitations?.(data as unknown as { citations: Citation[] });
        break;
      case 'delta':
        handlers.onDelta?.(data as unknown as { content: string });
        break;
      case 'done':
        handlers.onDone?.(data as unknown as { conversation_id: string; message_id: string });
        break;
      case 'error':
        handlers.onError?.(data as unknown as { message: string });
        break;
      default:
        break;
    }
  };

  // eslint-disable-next-line no-constant-condition
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 按行切分，保留最后一条未以换行结尾的行作为缓冲
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';

    for (const line of lines) {
      if (line.startsWith('event: ')) {
        currentEvent = line.slice(7).trim();
      } else if (line.startsWith('data: ')) {
        pendingData += line.slice(6);
      } else if (line === '') {
        // 空行 = 事件分隔：派发并重置
        dispatch(currentEvent, pendingData);
        currentEvent = '';
        pendingData = '';
      }
    }
  }
  // 流结束后若仍有未派发的事件，补派一次
  if (currentEvent && pendingData) {
    dispatch(currentEvent, pendingData);
  }
}

/**
 * React hook 版本：返回一个稳定引用的 streamSSE 调用函数。
 * 供组件内使用；Zustand store 等非组件场景直接 import { streamSSE }。
 */
export function useSSE() {
  return useCallback(
    (url: string, body: unknown, handlers: SSEHandlers) => streamSSE(url, body, handlers),
    [],
  );
}
