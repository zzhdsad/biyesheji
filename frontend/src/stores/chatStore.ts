import { create } from 'zustand';
import { streamSSE } from '@/hooks/useSSE';
import type { ChatMessage, Conversation, HealthResponse, KnowledgeBase } from '@/types';
import {
  createKb as apiCreateKb,
  deleteAllConversations as apiDeleteAllConversations,
  deleteConversation as apiDeleteConversation,
  fetchConversations,
  fetchHealth,
  fetchKnowledgeBases,
  fetchMessages,
} from '@/services/api';

interface ChatState {
  // 知识库
  knowledgeBases: KnowledgeBase[];
  /** 选中的知识库 ID 列表；空数组 = 全部知识库（默认） */
  selectedKbIds: string[];
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
  /** 创建知识库（成功后自动选中新库并刷新列表）。 */
  createKb: (name: string, description?: string, visibility?: 'public' | 'private') => Promise<KnowledgeBase | null>;
  /** 设置选中的知识库列表（空数组 = 全部）。持久化到 localStorage。 */
  setSelectedKbIds: (kbIds: string[]) => void;
  /** 重置为全部知识库（清空选择）。 */
  resetKbSelection: () => void;
  loadConversations: () => Promise<void>;
  selectConversation: (conversationId: string) => Promise<void>;
  newConversation: () => void;
  deleteConversation: (conversationId: string) => Promise<void>;
  deleteAllConversations: () => Promise<void>;
  sendMessage: (question: string) => Promise<void>;
  loadHealth: () => Promise<void>;
}

const KB_SELECTION_KEY = 'kp_selected_kb_ids';

/** 从 localStorage 读取上次选中的知识库 ID 列表。 */
function loadStoredKbIds(): string[] {
  try {
    const raw = localStorage.getItem(KB_SELECTION_KEY);
    if (raw) {
      const ids = JSON.parse(raw);
      if (Array.isArray(ids)) return ids;
    }
  } catch {
    // localStorage 不可用或数据损坏，忽略
  }
  return []; // 默认空 = 全部
}

/** 持久化选中的知识库 ID 列表到 localStorage。 */
function storeKbIds(ids: string[]): void {
  try {
    localStorage.setItem(KB_SELECTION_KEY, JSON.stringify(ids));
  } catch {
    // 忽略写入失败
  }
}

export const useChatStore = create<ChatState>((set, get) => ({
  knowledgeBases: [],
  selectedKbIds: loadStoredKbIds(),
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
      set((s) => {
        // 过滤掉已不存在的 KB ID（用户可能删除了知识库）
        const validIds = s.selectedKbIds.filter((id) =>
          kbs.some((kb) => kb.id === id),
        );
        return {
          knowledgeBases: kbs,
          selectedKbIds: validIds,
          loadingKbs: false,
        };
      });
    } catch {
      set({ loadingKbs: false });
    }
  },

  setSelectedKbIds: (kbIds: string[]) => {
    storeKbIds(kbIds);
    set({ selectedKbIds: kbIds });
  },

  resetKbSelection: () => {
    storeKbIds([]);
    set({ selectedKbIds: [] });
  },

  createKb: async (name, description, visibility) => {
    try {
      const kb = await apiCreateKb({ name, description, visibility });
      // 创建后立即把新库插入列表头部（不自动选中，保持用户当前选择）
      set((s) => ({
        knowledgeBases: [kb, ...s.knowledgeBases],
      }));
      return kb;
    } catch {
      return null;
    }
  },

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

  deleteConversation: async (conversationId: string) => {
    try {
      await apiDeleteConversation(conversationId);
    } catch {
      // 删除失败仍从列表中移除，避免幽灵会话残留
    }
    const { currentConversationId, conversations } = get();
    const filtered = conversations.filter((c) => c.id !== conversationId);
    set({ conversations: filtered });
    // 删除的是当前会话 → 清空消息视图
    if (currentConversationId === conversationId) {
      set({ currentConversationId: null, messages: [] });
    }
  },

  deleteAllConversations: async () => {
    try {
      await apiDeleteAllConversations();
    } catch {
      // 删除失败仍清空列表，避免幽灵残留
    }
    set({ conversations: [], currentConversationId: null, messages: [] });
  },

  sendMessage: async (question: string) => {
    const { selectedKbIds, knowledgeBases, currentConversationId } = get();
    // 空数组 = 全部知识库；否则用选中的。至少要有 1 个知识库才能提问。
    const allKbIds = knowledgeBases.map((kb) => kb.id);
    const kbIds = selectedKbIds.length > 0 ? selectedKbIds : allKbIds;
    if (kbIds.length === 0) return; // 无知识库时拒绝发送
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
        { question, kb_ids: kbIds, conversation_id: currentConversationId },
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
