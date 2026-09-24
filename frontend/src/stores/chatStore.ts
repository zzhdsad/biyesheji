import { create } from 'zustand';
import { streamSSE } from '@/hooks/useSSE';
import { createRequestSeq } from '@/utils/requestSeq';
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

/**
 * 会话视图序号（BUG-013）：切换会话 / 新建会话 / 删除会话都会作废在飞的
 * 消息加载与流式回调，避免"慢响应覆盖新会话""已删会话的消息复活"。
 * 注意：这只是状态守卫（丢弃过期写入），不负责取消请求——SSE 无 AbortController，
 * 继续跑的流因按 assistantId 匹配不到消息而自然无副作用。
 */
const conversationSeq = createRequestSeq();

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
    const reqId = conversationSeq.begin();
    set({ currentConversationId: conversationId, messages: [] });
    try {
      const msgs = await fetchMessages(conversationId);
      // BUG-013：期间若又切换/删除/清空了会话，本次响应已过期 → 丢弃
      if (!conversationSeq.isLatest(reqId)) return;
      set({ messages: msgs });
    } catch {
      // 加载失败保持空消息列表
    }
  },

  newConversation: () => {
    // BUG-013：作废在飞的旧会话消息加载
    conversationSeq.invalidate();
    set({ currentConversationId: null, messages: [] });
  },

  deleteConversation: async (conversationId: string) => {
    try {
      await apiDeleteConversation(conversationId);
    } catch {
      // 删除失败仍从列表中移除，避免幽灵会话残留
    }
    // BUG-013：会话已删除，在飞的消息加载不得再写回消息区（旧实现会让
    // 已删会话的历史消息"复活"在被清空的消息区）
    conversationSeq.invalidate();
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
    conversationSeq.invalidate(); // BUG-013：同 deleteConversation
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

    // BUG-013：记录本次发送时的会话视图；期间用户切走/删除会话后，
    // 流式回调不得再把 currentConversationId 写回这条旧流的会话。
    const viewId = conversationSeq.current();

    try {
      await streamSSE(
        '/api/v1/chat/ask-stream',
        { question, kb_ids: kbIds, conversation_id: currentConversationId },
        {
          onStart: ({ conversation_id, query_analysis, router_decision }) => {
            // 阶段十一/十二：start 事件携带 Query 分析与路由决策（仅展示，不影响请求行为）
            set((s) => ({
              currentConversationId: conversationSeq.isLatest(viewId)
                ? conversation_id
                : s.currentConversationId,
              messages:
                query_analysis || router_decision
                  ? s.messages.map((m) =>
                      m.id === assistantId
                        ? {
                            ...m,
                            ...(query_analysis ? { queryAnalysis: query_analysis } : {}),
                            ...(router_decision ? { routerDecision: router_decision } : {}),
                          }
                        : m,
                    )
                  : s.messages,
            }));
          },
          onCitations: ({
            citations,
            evidence,
            evidence_groups,
            evidence_summary,
            kg_evidence,
            evidence_gate,
          }) => {
            // 引用卡片在生成前实时展示；阶段十：同时保存多来源证据分组
            // 阶段十三：保存 KG 证据切片（与 citations 同源，供后续区分展示）
            // 阶段十四：附带保存 Evidence Gate 决策（暂不展示，仅留待后续阶段）
            set((s) => ({
              messages: s.messages.map((m) =>
                m.id === assistantId
                  ? {
                      ...m,
                      citations,
                      evidence: evidence ?? citations,
                      evidenceGroups: evidence_groups,
                      evidenceSummary: evidence_summary,
                      ...(kg_evidence ? { kgEvidence: kg_evidence } : {}),
                      ...(evidence_gate ? { evidenceGate: evidence_gate } : {}),
                    }
                  : m,
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
          onDone: ({ message_id, conversation_id, answer, reflection }) => {
            set((s) => ({
              messages: s.messages.map((m) =>
                m.id === assistantId
                  ? {
                      ...m,
                      id: message_id || m.id,
                      // 阶段十五：Reflection 可能把流式答案改写为更保守的版本，
                      // done 事件带回最终权威文本（后端已按该文本持久化）
                      ...(answer ? { content: answer } : {}),
                      ...(reflection ? { reflection } : {}),
                    }
                  : m,
              ),
              currentConversationId: conversationSeq.isLatest(viewId)
                ? conversation_id
                : s.currentConversationId,
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
    } finally {
      // 阶段十六：流异常终止（既没收到 done 也没收到 error，例如连接被中断）时，
      // 必须恢复可发送状态，否则 sending 永久为 true，用户再也无法提问。
      // 已流式渲染出的内容保留，交由用户判断是否重发。
      set((s) => (s.sending ? { sending: false } : s));
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
