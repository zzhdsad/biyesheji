'use client';

import { useCallback } from 'react';
import type {
  Citation,
  EvidenceGroup,
  EvidenceSummary,
  GateDecision,
  QueryAnalysis,
  ReflectionDecision,
  RouterDecision,
} from '@/types';
import { getToken, handleUnauthorized } from '@/services/token';

/** SSE 事件回调集合（与后端 chat.py /ask-stream 事件协议对应）。 */
export interface SSEHandlers {
  /**
   * 阶段十一/十二：start 事件在原 conversation_id 之外附带 query_analysis 与
   * router_decision（事件名/顺序不变）。
   */
  onStart?: (data: {
    conversation_id: string;
    query_analysis?: QueryAnalysis;
    router_decision?: RouterDecision;
  }) => void;
  /** 阶段十/十四：citations 事件在原 citations 之外附带多来源证据结构与门控决策。 */
  onCitations?: (data: {
    citations: Citation[];
    evidence?: Citation[];
    evidence_groups?: EvidenceGroup[];
    evidence_summary?: EvidenceSummary;
    /** 阶段十三：KG 证据切片 */
    kg_evidence?: Citation[];
    /** 阶段十四：Evidence Gate 决策（Gate 关闭时为 null） */
    evidence_gate?: GateDecision | null;
  }) => void;
  onDelta?: (data: { content: string }) => void;
  // 阶段十五：done 事件额外下发最终答案与自反思决策
  // （流式答案可能被 revise 改写，answer 为最终权威文本；事件名与顺序不变）
  onDone?: (data: {
    conversation_id: string;
    message_id: string;
    answer?: string;
    reflection?: ReflectionDecision | null;
  }) => void;
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
  // BUG-062：流式 fetch 不过 axios 拦截器，401 必须在这里走同一套处理
  // （清 token + 硬跳登录页），否则 token 过期只表现为"请求失败"。
  if (resp.status === 401) {
    handleUnauthorized();
    throw new Error('登录已过期，请重新登录');
  }
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
  // 流结束后仍有未处理的内容：先消费最后一行（未以换行结尾的行仍在 buffer 中），
  // 再补派一次未结束的事件（阶段十六修复：尾部分块缺少换行时最后一个事件会被丢弃）
  const tailLine = buffer;
  if (tailLine.startsWith('event: ')) {
    currentEvent = tailLine.slice(7).trim();
  } else if (tailLine.startsWith('data: ')) {
    pendingData += tailLine.slice(6);
  }
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
