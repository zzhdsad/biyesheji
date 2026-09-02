import { create } from 'zustand';
import { streamSSE } from '@/hooks/useSSE';
import type { ChatMessage, Conversation, HealthResponse, KnowledgeBase } from '@/types';
import {
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
    // 预声明助手占位消息 id，便于流式增量更新（打字机效果）
    const assistantId = crypto.randomUUID();
    set((s) => ({
      messages: [
        ...s.messages,
        userMsg,
        { id: assistantId, role: 'assistant', content: '', citations: [] },
      ],
      sending: true,
    }));

    try {
      await streamSSE(
        '/api/v1/chat/ask-stream',
        { question, kb_ids: [selectedKbId], conversation_id: currentConversationId },
        {
          onStart: ({ conversation_id }) => {
            set({ currentConversationId: conversation_id });
          },
          onCitations: ({ citations }) => {
            // 引用卡片在生成前实时展示
            set((s) => ({
              messages: s.messages.map((m) =>
                m.id === assistantId ? { ...m, citations } : m,
              ),
            }));
          },
          onDelta: ({ content }) => {
            // 逐 chunk 追加内容，形成打字机效果
            set((s) => ({
              messages: s.messages.map((m) =>
                m.id === assistantId ? { ...m, content: m.content + content } : m,
              ),
            }));
          },
          onDone: ({ message_id, conversation_id }) => {
            set((s) => ({
              messages: s.messages.map((m) =>
                m.id === assistantId ? { ...m, id: message_id || m.id } : m,
              ),
              currentConversationId: conversation_id,
              sending: false,
            }));
            // 发送成功后刷新侧边栏会话列表（标题/排序变化）
            void get().loadConversations();
          },
          onError: ({ message }) => {
            set((s) => ({
              messages: s.messages.map((m) =>
                m.id === assistantId
                  ? { ...m, content: m.content || `生成失败：${message}` }
                  : m,
              ),
              sending: false,
            }));
          },
        },
      );
    } catch {
      // 网络错误等：占位消息兜底提示
      set((s) => ({
        messages: s.messages.map((m) =>
          m.id === assistantId
            ? { ...m, content: m.content || '请求失败，请稍后重试。' }
            : m,
        ),
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
