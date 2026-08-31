import { create } from 'zustand';
import type { ChatMessage, Conversation, HealthResponse, KnowledgeBase } from '@/types';
import {
  askQuestion,
  fetchConversations,
  fetchHealth,
  fetchKnowledgeBases,
  fetchMessages,
} from '@/services/api';

interface ChatState {
  // 知识库
  knowledgeBases: KnowledgeBase[];
  selectedKbId: string | null;
  // 会话
  conversations: Conversation[];
  currentConversationId: string | null;
  messages: ChatMessage[];
  // 状态
  loadingKbs: boolean;
  loadingConversations: boolean;
  sending: boolean;
  health: HealthResponse | null;
  // 动作
  loadKnowledgeBases: () => Promise<void>;
  selectKb: (kbId: string) => void;
  loadConversations: () => Promise<void>;
  selectConversation: (conversationId: string) => Promise<void>;
  newConversation: () => void;
  sendMessage: (question: string) => Promise<void>;
  loadHealth: () => Promise<void>;
}

export const useChatStore = create<ChatState>((set, get) => ({
  knowledgeBases: [],
  selectedKbId: null,
  conversations: [],
  currentConversationId: null,
  messages: [],
  loadingKbs: false,
  loadingConversations: false,
  sending: false,
  health: null,

  loadKnowledgeBases: async () => {
    set({ loadingKbs: true });
    try {
      const kbs = await fetchKnowledgeBases();
      set((s) => ({
        knowledgeBases: kbs,
        // 未选时默认选第一个，保证问答有知识库范围
        selectedKbId: s.selectedKbId ?? kbs[0]?.id ?? null,
        loadingKbs: false,
      }));
    } catch {
      set({ loadingKbs: false });
    }
  },

  selectKb: (kbId: string) => set({ selectedKbId: kbId }),

  loadConversations: async () => {
    set({ loadingConversations: true });
    try {
      const list = await fetchConversations();
      set({ conversations: list, loadingConversations: false });
    } catch {
      set({ loadingConversations: false });
    }
  },

  selectConversation: async (conversationId: string) => {
    set({ currentConversationId: conversationId, messages: [] });
    try {
      const msgs = await fetchMessages(conversationId);
      set({ messages: msgs });
    } catch {
      // 加载失败保持空消息列表
    }
  },

  newConversation: () => set({ currentConversationId: null, messages: [] }),

  sendMessage: async (question: string) => {
    const { selectedKbId, currentConversationId } = get();
    if (!selectedKbId) return; // 无知识库范围时拒绝发送（权限约束）
    if (get().sending) return; // 防止重复发送

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: question,
    };
    set((s) => ({ messages: [...s.messages, userMsg], sending: true }));

    try {
      const res = await askQuestion({
        question,
        kb_ids: [selectedKbId],
        conversation_id: currentConversationId,
      });
      const assistantMsg: ChatMessage = {
        id: res.message_id ?? crypto.randomUUID(),
        role: 'assistant',
        content: res.answer,
        citations: res.citations,
      };
      set((s) => ({
        messages: [...s.messages, assistantMsg],
        currentConversationId: res.conversation_id,
        sending: false,
      }));
      // 发送成功后刷新侧边栏会话列表（标题/排序变化）
      void get().loadConversations();
    } catch {
      set((s) => ({
        messages: [
          ...s.messages,
          { id: crypto.randomUUID(), role: 'assistant', content: '请求失败，请稍后重试。' },
        ],
        sending: false,
      }));
    }
  },

  loadHealth: async () => {
    try {
      const h = await fetchHealth();
      set({ health: h });
    } catch {
      set({ health: null });
    }
  },
}));
