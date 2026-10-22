import type { NextRequest } from 'next/server';

/**
 * SSE 流式代理（BUG：Next 生产服务器的 rewrites 代理会缓冲整个响应体，
 * 导致 /chat/ask-stream 的 122 个 delta 在流结束后一次性到达——用户看到
 * "等几十秒后回答一次性出现"，SSE 完全失效）。
 *
 * 修复：改用 Route Handler 显式转发 upstream 的 ReadableStream
 * （Next 对 Route Handler 响应不缓冲），恢复逐 chunk 推送。
 *
 * 匹配顺序：文件路由优先于 afterFiles rewrites（next.config.mjs 中
 * /api/v1/* 的通用代理仍兜底其余非流式端点，无需改动）。
 *
 * 透传：状态码（401/403/422 原样到达前端登录/权限处理）、Authorization 头。
 */
export const dynamic = 'force-dynamic';
export const maxDuration = 300;

const BACKEND_URL =
  process.env.BACKEND_URL_INTERNAL ?? process.env.BACKEND_URL ?? 'http://localhost:8000';

export async function POST(req: NextRequest) {
  const authorization = req.headers.get('authorization');
  const upstream = await fetch(`${BACKEND_URL}/api/v1/chat/ask-stream`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(authorization ? { authorization } : {}),
      accept: 'text/event-stream',
    },
    body: await req.text(),
    cache: 'no-store',
  });

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: {
      'content-type': 'text/event-stream; charset=utf-8',
      'cache-control': 'no-cache, no-transform',
      connection: 'keep-alive',
      'x-accel-buffering': 'no',
    },
  });
}
