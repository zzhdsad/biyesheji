'use client';

import { useState } from 'react';
import { App, Button, Input, Modal } from 'antd';
import {
  LikeFilled,
  LikeOutlined,
  DislikeFilled,
  DislikeOutlined,
} from '@ant-design/icons';
import { fetchFeedback, submitFeedback } from '@/services/api';

const { TextArea } = Input;

/**
 * 反馈按钮组（PRD §3.6 / §8：用户对助手消息点赞/踩 + 文本纠错意见收集）。
 *
 * 行为：
 * - 点击 👍/👎 弹出 Modal，用户可选填纠错意见后提交
 * - 同一消息重复提交时后端覆盖更新，前端立即反映最新选中态
 * - 初始挂载时按 message_id 拉取已有反馈以恢复高亮（消息流复用时尤为重要）
 */
export function FeedbackButtons({ messageId }: { messageId: string }) {
  const { message } = App.useApp();
  const [rating, setRating] = useState<1 | -1 | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const [pendingRating, setPendingRating] = useState<1 | -1>(1);
  const [comment, setComment] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // 惰性拉取已有反馈：首次 hover/click 时拉，避免每个消息都请求
  const ensureLoaded = async () => {
    if (loaded) return;
    try {
      const existing = await fetchFeedback(messageId);
      if (existing) {
        setRating(existing.rating === 1 ? 1 : -1);
        setComment(existing.comment ?? '');
      }
    } catch {
      // 静默失败：不影响主流程
    } finally {
      setLoaded(true);
    }
  };

  const openModal = async (r: 1 | -1) => {
    await ensureLoaded();
    setPendingRating(r);
    // 切换目标 rating 时预填已有意见（若之前已反馈过）
    setComment((c) => (c ? c : ''));
    setOpen(true);
  };

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      await submitFeedback({
        message_id: messageId,
        rating: pendingRating,
        comment: comment.trim(),
      });
      setRating(pendingRating);
      setOpen(false);
      message.success(pendingRating === 1 ? '感谢点赞，已记录' : '感谢反馈，已记录');
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail ?? '提交失败，请稍后再试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}
      onMouseEnter={ensureLoaded}
    >
      <Button
        type="text"
        size="small"
        aria-label="点赞"
        onClick={() => openModal(1)}
        icon={
          rating === 1 ? (
            <LikeFilled style={{ color: '#52c41a' }} />
          ) : (
            <LikeOutlined style={{ color: '#8c8c8c' }} />
          )
        }
      />
      <Button
        type="text"
        size="small"
        aria-label="纠错反馈"
        onClick={() => openModal(-1)}
        icon={
          rating === -1 ? (
            <DislikeFilled style={{ color: '#ff4d4f' }} />
          ) : (
            <DislikeOutlined style={{ color: '#8c8c8c' }} />
          )
        }
      />

      <Modal
        title={pendingRating === 1 ? '点赞反馈' : '纠错反馈'}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleSubmit}
        okText="提交"
        cancelText="取消"
        confirmLoading={submitting}
        destroyOnClose
      >
        <p style={{ color: 'rgba(0,0,0,0.65)', marginBottom: 8 }}>
          {pendingRating === 1
            ? '这条回答对你有帮助，可补充意见帮助持续优化。'
            : '这条回答有不完整或错误，请补充说明以便后续改进。'}
        </p>
        <TextArea
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="可选：补充纠错意见（最多 2000 字）"
          autoSize={{ minRows: 3, maxRows: 6 }}
          maxLength={2000}
          showCount
        />
      </Modal>
    </div>
  );
}
