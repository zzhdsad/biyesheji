import { create } from 'zustand';
import type { DocumentItem, KnowledgeBase } from '@/types';
import {
  deleteDocument,
  fetchDocumentsPage,
  fetchKnowledgeBases,
  reindexDocument,
  reparseDocument,
} from '@/services/api';

/** 是否处于解析中（需轮询刷新）。 */
export const isParsing = (s: string) => s === 'pending' || s === 'parsing';

interface DocumentState {
  // 知识库（筛选与上传目标）
  knowledgeBases: KnowledgeBase[];
  selectedKbId: string | null;
  // 文档列表
  documents: DocumentItem[];
  total: number;
  page: number;
  pageSize: number;
  // 状态
  loadingKbs: boolean;
  loading: boolean;
  error: string | null;
  // 动作
  loadKnowledgeBases: () => Promise<void>;
  selectKb: (kbId: string) => void;
  loadDocuments: (page?: number, pageSize?: number) => Promise<void>;
  removeDocument: (docId: string) => Promise<void>;
  reparse: (docId: string) => Promise<void>;
  reindex: (docId: string) => Promise<void>;
  clearError: () => void;
}

export const useDocumentStore = create<DocumentState>((set, get) => ({
  knowledgeBases: [],
  selectedKbId: null,
  documents: [],
  total: 0,
  page: 1,
  pageSize: 10,
  loadingKbs: false,
  loading: false,
  error: null,

  loadKnowledgeBases: async () => {
    set({ loadingKbs: true });
    try {
      const kbs = await fetchKnowledgeBases();
      set((s) => ({
        knowledgeBases: kbs,
        // 未选时默认选第一个，保证上传有目标知识库
        selectedKbId: s.selectedKbId ?? kbs[0]?.id ?? null,
        loadingKbs: false,
      }));
    } catch {
      set({ loadingKbs: false });
    }
  },

  selectKb: (kbId: string) => {
    set({ selectedKbId: kbId, page: 1 });
    void get().loadDocuments(1, get().pageSize);
  },

  loadDocuments: async (page, pageSize) => {
    const nextPage = page ?? get().page;
    const nextSize = pageSize ?? get().pageSize;
    set({ loading: true, error: null, page: nextPage, pageSize: nextSize });
    try {
      const { items, total } = await fetchDocumentsPage(
        get().selectedKbId ?? undefined,
        nextSize,
        (nextPage - 1) * nextSize,
      );
      set({ documents: items, total, loading: false });
    } catch {
      set({ loading: false, error: '文档列表加载失败' });
    }
  },

  removeDocument: async (docId: string) => {
    try {
      await deleteDocument(docId);
      const remaining = get().documents.filter((d) => d.id !== docId);
      set((s) => ({ documents: remaining, total: Math.max(0, s.total - 1) }));
      if (remaining.length === 0 && get().page > 1) {
        void get().loadDocuments(get().page - 1, get().pageSize);
      }
    } catch {
      set({ error: '删除失败，请稍后重试' });
    }
  },

  reparse: async (docId: string) => {
    try {
      const doc = await reparseDocument(docId);
      // 后端派发解析任务后立即返回，本地先置为 parsing 以便轮询刷新
      set((s) => ({
        documents: s.documents.map((d) =>
          d.id === docId ? { ...doc, parse_status: 'parsing' as const } : d,
        ),
      }));
    } catch {
      set({ error: '重新解析失败，请稍后重试' });
    }
  },

  reindex: async (docId: string) => {
    try {
      const doc = await reindexDocument(docId);
      set((s) => ({
        documents: s.documents.map((d) => (d.id === docId ? doc : d)),
      }));
    } catch {
      set({ error: '重新向量化失败：可能切片未就绪，请先完成解析' });
    }
  },

  clearError: () => set({ error: null }),
}));
