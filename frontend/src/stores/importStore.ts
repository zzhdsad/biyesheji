'use client';

import { create } from 'zustand';
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware';

import type {
  ForeignKeyItem,
  GeneralCommitResult,
  ImportProgressSnapshot,
  ImportTemplateMatchResult,
  JunctionItem,
  RelationalCommitResult,
  RelationalTable,
  ScannedFile,
} from '@/services/api';

/**
 * 导入页的长期状态（会话级持久化）。
 *
 * 导入是长任务：用户切到其他页面再回来，分析结果、映射选择和提交进度
 * 都必须还在；提交期间刷新页面也要能通过 session_id 轮询恢复。
 *
 * - 存 sessionStorage：每个标签页独立，不会串掉另一个标签页的导入；
 * - 不持久化文件对象（File 不可序列化）和一次性 UI 开关；
 * - `committing` 需要持久化：重新挂载后据此恢复进度轮询。
 */
const RELATIONAL_KEY = 'kp-import-relational';
const NORMAL_KEY = 'kp-import-normal';

/**
 * sessionStorage 包装：SSR 无该对象、大目录可能超配额。
 * 持久化失败只丢“切页恢复”，不能让导入操作本身抛错。
 */
const safeSessionStorage: StateStorage = {
  getItem: (name) => {
    try {
      return sessionStorage.getItem(name);
    } catch {
      return null;
    }
  },
  setItem: (name, value) => {
    try {
      sessionStorage.setItem(name, value);
    } catch (error) {
      console.warn('[importStore] 导入状态持久化失败（本次页面内仍可用）：', error);
    }
  },
  removeItem: (name) => {
    try {
      sessionStorage.removeItem(name);
    } catch {
      // 忽略：清不掉也不影响当前页面状态
    }
  },
};

export interface FileSetting {
  entity_type: string;
  pk_field: string;
}

export interface RelationalImportState {
  kbId?: string;
  sessionId: string;
  tables: RelationalTable[];
  primaryKeys: Record<string, string>;
  foreignKeys: ForeignKeyItem[];
  junctions: JunctionItem[];
  analyzeErrors: string[];
  selectedFks: string[];
  selectedJunctions: string[];
  relationTypes: Record<string, string>;
  templateFingerprint: string;
  templateMatch: ImportTemplateMatchResult | null;
  appliedTemplateId: string | null;
  autoConfirm: boolean;
  committing: boolean;
  commitProgress: ImportProgressSnapshot | null;
  commitResult?: RelationalCommitResult;
}

interface RelationalImportStore extends RelationalImportState {
  patch: (patch: Partial<RelationalImportState>) => void;
  reset: () => void;
}

export interface NormalImportState {
  kbId?: string;
  sessionId: string;
  scanned: ScannedFile[];
  scanErrors: string[];
  settings: Record<string, FileSetting>;
  committing: boolean;
  commitProgress: ImportProgressSnapshot | null;
  result?: GeneralCommitResult;
}

interface NormalImportStore extends NormalImportState {
  patch: (patch: Partial<NormalImportState>) => void;
  reset: () => void;
}

const RELATIONAL_DEFAULTS: RelationalImportState = {
  kbId: undefined,
  sessionId: '',
  tables: [],
  primaryKeys: {},
  foreignKeys: [],
  junctions: [],
  analyzeErrors: [],
  selectedFks: [],
  selectedJunctions: [],
  relationTypes: {},
  templateFingerprint: '',
  templateMatch: null,
  appliedTemplateId: null,
  autoConfirm: false,
  committing: false,
  commitProgress: null,
  commitResult: undefined,
};

const NORMAL_DEFAULTS: NormalImportState = {
  kbId: undefined,
  sessionId: '',
  scanned: [],
  scanErrors: [],
  settings: {},
  committing: false,
  commitProgress: null,
  result: undefined,
};

export const useRelationalImportStore = create<RelationalImportStore>()(
  persist(
    (set) => ({
      ...RELATIONAL_DEFAULTS,
      patch: (patch) => set(patch),
      reset: () => set(RELATIONAL_DEFAULTS),
    }),
    {
      name: RELATIONAL_KEY,
      storage: createJSONStorage(() => safeSessionStorage),
      partialize: (state): RelationalImportState => ({
        kbId: state.kbId,
        sessionId: state.sessionId,
        tables: state.tables,
        primaryKeys: state.primaryKeys,
        foreignKeys: state.foreignKeys,
        junctions: state.junctions,
        analyzeErrors: state.analyzeErrors,
        selectedFks: state.selectedFks,
        selectedJunctions: state.selectedJunctions,
        relationTypes: state.relationTypes,
        templateFingerprint: state.templateFingerprint,
        templateMatch: state.templateMatch,
        appliedTemplateId: state.appliedTemplateId,
        autoConfirm: state.autoConfirm,
        committing: state.committing,
        commitProgress: state.commitProgress,
        commitResult: state.commitResult,
      }),
    },
  ),
);

export const useNormalImportStore = create<NormalImportStore>()(
  persist(
    (set) => ({
      ...NORMAL_DEFAULTS,
      patch: (patch) => set(patch),
      reset: () => set(NORMAL_DEFAULTS),
    }),
    {
      name: NORMAL_KEY,
      storage: createJSONStorage(() => safeSessionStorage),
      partialize: (state): NormalImportState => ({
        kbId: state.kbId,
        sessionId: state.sessionId,
        scanned: state.scanned,
        scanErrors: state.scanErrors,
        settings: state.settings,
        committing: state.committing,
        commitProgress: state.commitProgress,
        result: state.result,
      }),
    },
  ),
);
