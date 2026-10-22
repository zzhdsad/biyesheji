'use client';

import { useEffect, useState } from 'react';

const ENTRY_KEY = 'kp_entry_played';

/**
 * 应用入口过场动画：仅每个浏览器会话首次进入时播放一次。
 * 纯 CSS 动画，不依赖图片或第三方库；sessionStorage 不可用时只跳过持久化。
 */
export function EntryAnimation() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    let played = false;
    try {
      played = window.sessionStorage.getItem(ENTRY_KEY) === '1';
    } catch {
      // 隐私模式 / 存储禁用：仍然播放一次
    }
    if (played) return;

    setVisible(true);
    const timer = window.setTimeout(() => {
      setVisible(false);
      try {
        window.sessionStorage.setItem(ENTRY_KEY, '1');
      } catch {
        // 忽略存储失败
      }
    }, 1900);
    return () => window.clearTimeout(timer);
  }, []);

  if (!visible) return null;

  return (
    <div className="entry-overlay" aria-hidden="true">
      <div className="entry-grid" />
      <div className="entry-core">
        <div className="entry-ring entry-ring-outer" />
        <div className="entry-ring entry-ring-inner" />
        <div className="entry-mark">中</div>
      </div>
      <div className="entry-copy">
        <span className="entry-kicker">TCM KNOWLEDGE CORE</span>
        <span className="entry-title">中医药知识中枢</span>
        <span className="entry-status">INITIALIZING KNOWLEDGE GRAPH · RAG ONLINE</span>
      </div>
      <div className="entry-scanline" />
    </div>
  );
}
