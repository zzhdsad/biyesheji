import { create } from 'zustand';
import type { ChatMessage } from '@/types';
import { askQuestion } from '@/services/api';

interface ChatState {
  messages: ChatMessage[];
  currentConversationId: string | null;
  sendMessage: (question: string) => Promise<void>;
  reset: () => void;
}

export const useChatStore = create<ChatState>((set) => ({
  messages: [],
  currentConversationId: null,

  sendMessage: async (question: string) => {
    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: question,
    };
    set((s) => ({ messages: [...s.messages, userMsg] }));

    try {
      const res = await askQuestion({ question });
      const assistantMsg: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: res.answer,
        citations: res.citations,
      };
      set((s) => ({
        messages: [...s.messages, assistantMsg],
        currentConversationId: res.conversation_id,
      }));
    } catch {
      set((s) => ({
        messages: [
          ...s.messages,
          { id: crypto.randomUUID(), role: 'assistant', content: '请求失败，请稍后重试。' },
        ],
      }));
    }
  },

  reset: () => set({ messages: [], currentConversationId: null }),
}));
