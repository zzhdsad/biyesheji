import { create } from 'zustand';
import type { DocumentItem, FileFolderItem, FileSpace, KnowledgeBase } from '@/types';
import {
  deleteDocument,
  fetchDocumentsPage,
  fetchFolders,
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
  // 文件空间：普通文档 / 结构化数据，各自一棵文件夹树
  space: FileSpace;
  /** 结构化空间内的展示范围：false = 非关联性数据，true = 关联性数据（文件组成员） */
  relatedScope: boolean;
  folders: FileFolderItem[];
  /** 当前所在文件夹；null = 空间根目录 */
  currentFolderId: string | null;
  // 文档列表
  documents: DocumentItem[];
  total: number;
  page: number;
  pageSize: number;
  // 状态
  loadingKbs: boolean;
  loading: boolean;
  loadingFolders: boolean;
  error: string | null;
  // 动作
  loadKnowledgeBases: () => Promise<void>;
  selectKb: (kbId: string) => void;
  loadDocuments: (page?: number, pageSize?: number, silent?: boolean) => Promise<void>;
  loadFolders: () => Promise<void>;
  /** 切换 Tab：换空间并回到该空间根目录 */
  setSpace: (space: FileSpace) => void;
  /** 切换结构化空间的展示范围（关联 / 非关联），必要时重新加载列表与目录计数 */
  setStructuredScope: (related: boolean) => void;
  /** 进入文件夹 / 返回上级（null = 空间根目录） */
  openFolder: (folderId: string | null) => void;
  removeDocument: (docId: string) => Promise<void>;
  reparse: (docId: string) => Promise<void>;
  reindex: (docId: string) => Promise<void>;
  clearError: () => void;
}

export const useDocumentStore = create<DocumentState>((set, get) => ({
  knowledgeBases: [],
  selectedKbId: null,
  space: 'document',
  relatedScope: false,
  folders: [],
  currentFolderId: null,
  documents: [],
  total: 0,
  page: 1,
  pageSize: 10,
  loadingKbs: false,
  loading: false,
  loadingFolders: false,
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

  loadDocuments: async (page, pageSize, silent = false) => {
    const state = get();
    const nextPage = page ?? state.page;
    const nextSize = pageSize ?? state.pageSize;
    // 轮询刷新用 silent：不切 loading，避免表格每 4 秒闪一次转圈
    set(
      silent
        ? { error: null, page: nextPage, pageSize: nextSize }
        : { loading: true, error: null, page: nextPage, pageSize: nextSize },
    );
    try {
      const { items, total } = await fetchDocumentsPage({
        // 文件夹视图展示目录内全部文件（知识库筛选只在空间根目录生效）：
        // 文件夹是跨知识库的全局树，若在目录内继续按 kb 过滤，会出现
        // 「目录标着 3 个文件、列表却为空」的假丢失。
        kbId: state.currentFolderId ? undefined : (state.selectedKbId ?? undefined),
        space: state.space,
        related: state.space === 'structured' ? state.relatedScope : undefined,
        folderId: state.currentFolderId ?? undefined,
        rootOnly: state.currentFolderId ? false : true,
        limit: nextSize,
        offset: (nextPage - 1) * nextSize,
      });
      set({ documents: items, total, loading: false });
    } catch {
      set({ loading: false, error: '文档列表加载失败' });
    }
  },

  loadFolders: async () => {
    set({ loadingFolders: true });
    try {
      const folders = await fetchFolders(
        get().space,
        get().space === 'structured' ? get().relatedScope : undefined,
      );
      set({ folders, loadingFolders: false });
    } catch {
      // 文件夹拉取失败不弹全局错误：列表区已有错误提示，避免两个请求重复报同一个错
      set({ loadingFolders: false });
    }
  },

  setSpace: (space: FileSpace) => {
    if (space === get().space) return;
    set({ space, currentFolderId: null, page: 1, documents: [], folders: [] });
    void get().loadFolders();
    void get().loadDocuments(1, get().pageSize);
  },

  setStructuredScope: (related: boolean) => {
    const state = get();
    if (state.space !== 'structured') {
      // 从普通文档页签直接切到结构化页签：先记范围，再由 setSpace 一并加载
      set({ relatedScope: related });
      state.setSpace('structured');
      return;
    }
    if (state.relatedScope === related) return;
    set({
      relatedScope: related,
      currentFolderId: null,
      page: 1,
      documents: [],
      folders: [],
    });
    void get().loadFolders();
    void get().loadDocuments(1, get().pageSize);
  },

  openFolder: (folderId: string | null) => {
    set({ currentFolderId: folderId, page: 1 });
    void get().loadDocuments(1, get().pageSize);
  },

  removeDocument: async (docId: string) => {
    try {
      await deleteDocument(docId);
      const remaining = get().documents.filter((d) => d.id !== docId);
      set((s) => ({ documents: remaining, total: Math.max(0, s.total - 1) }));
      if (remaining.length === 0 && get().page > 1) {
        void get().loadDocuments(get().page - 1, get().pageSize);
      }
      // 目录的文件计数随之变化，刷新文件夹列表
      void get().loadFolders();
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
