'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { Spin } from 'antd';
import { useUserStore } from '@/stores/userStore';
import { getToken } from '@/services/token';

/**
 * 鉴权初始化包装器：应用启动时从本地存储恢复登录态（调 /me 校验 token）。
 *
 * middleware 在 SSR 侧已用 cookie 做了页面级保护；这里负责客户端 store 与
 * 后端的一致性：有 token 则乐观恢复 → /me 校验 → 失败清态（由响应拦截器跳登录）。
 *
 * 仅当本地存在 token 时才显示 loading（需要等待 /me 校验）；无 token 时直接渲染
 * 子内容，避免 /login、/register 出现多余的 spinner 闪烁。
 */
export function AuthInit({ children }: { children: ReactNode }) {
  const initializing = useUserStore((s) => s.initializing);
  const init = useUserStore((s) => s.init);
  // 仅客户端、仅当存在 token 时才需要阻塞渲染等待 /me 校验
  const [hasToken, setHasToken] = useState(false);

  useEffect(() => {
    setHasToken(!!getToken());
    void init();
  }, [init]);

  if (hasToken && initializing) {
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#f5f7fa',
        }}
      >
        <Spin size="large" />
      </div>
    );
  }

  return <>{children}</>;
}
