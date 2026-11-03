/**
 * 导入页状态持久化测试。
 *
 * 验证切页/刷新后的核心恢复能力：
 * - 分析结果、映射选择、提交中标记写入 sessionStorage；
 * - reset() 回到初始状态（下一次导入不残留上次数据）；
 * - 普通导入的扫描结果与实体/主键设置同样持久化。
 */
import assert from 'node:assert/strict';
import { before, beforeEach, describe, it } from 'node:test';

class FakeStorage {
  private data = new Map<string, string>();
  getItem(key: string) {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.data.set(key, String(value));
  }
  removeItem(key: string) {
    this.data.delete(key);
  }
  clear() {
    this.data.clear();
  }
}

const storage = new FakeStorage();
(globalThis as unknown as Record<string, unknown>).sessionStorage = storage;

// 必须在 sessionStorage 就绪后再加载 store，persist 初始化时才会挂上存储
type StoreModule = typeof import('@/stores/importStore');
let stores: StoreModule;

before(async () => {
  stores = await import('@/stores/importStore');
});

const RELATIONAL_KEY = 'kp-import-relational';
const NORMAL_KEY = 'kp-import-normal';

function persisted(key: string): Record<string, unknown> {
  const raw = storage.getItem(key);
  assert.ok(raw, `${key} 未写入 sessionStorage`);
  return JSON.parse(raw).state as Record<string, unknown>;
}

beforeEach(() => {
  storage.clear();
  stores.useRelationalImportStore.getState().reset();
  stores.useNormalImportStore.getState().reset();
});

describe('关联导入状态持久化', () => {
  it('分析结果与提交中标记写入 sessionStorage', () => {
    stores.useRelationalImportStore.getState().patch({
      kbId: 'kb-1',
      sessionId: 'sess-1',
      tables: [{ table: 'T1' } as never],
      primaryKeys: { T1: 'id' },
      foreignKeys: [],
      junctions: [],
      selectedFks: [],
      selectedJunctions: [],
      relationTypes: { 'T1.id': 'belongs_to' },
      templateFingerprint: 'fp',
      templateMatch: null,
      appliedTemplateId: null,
      autoConfirm: true,
      committing: true,
      commitProgress: {
        session_id: 'sess-1',
        phase: 'write',
        percent: 42,
        message: '写入中',
        done: false,
        failed: false,
      },
    });

    const state = persisted(RELATIONAL_KEY);
    assert.equal(state.sessionId, 'sess-1');
    assert.equal(state.autoConfirm, true);
    assert.equal(state.committing, true);
    assert.deepEqual(state.primaryKeys, { T1: 'id' });
    assert.equal((state.commitProgress as { percent: number }).percent, 42);
  });

  it('reset 清空会话数据，避免下次导入残留', () => {
    stores.useRelationalImportStore.getState().patch({
      sessionId: 'sess-1',
      tables: [{ table: 'T1' } as never],
      committing: true,
    });
    stores.useRelationalImportStore.getState().reset();
    const state = stores.useRelationalImportStore.getState();
    assert.equal(state.sessionId, '');
    assert.deepEqual(state.tables, []);
    assert.equal(state.committing, false);
    assert.equal(persisted(RELATIONAL_KEY).sessionId, '');
  });
});

describe('普通导入状态持久化', () => {
  it('扫描结果与实体/主键设置可恢复', () => {
    stores.useNormalImportStore.getState().patch({
      kbId: 'kb-1',
      sessionId: 'sess-2',
      scanned: [{ path: 'a.csv', detected_format: 'csv' } as never],
      scanErrors: [],
      settings: { 'a.csv': { entity_type: 'A', pk_field: 'id' } },
      committing: false,
    });

    const state = persisted(NORMAL_KEY);
    assert.equal(state.sessionId, 'sess-2');
    assert.deepEqual(state.settings, { 'a.csv': { entity_type: 'A', pk_field: 'id' } });
    assert.equal((state.scanned as unknown[]).length, 1);
  });

  it('导入结果在页面切回后仍可展示', () => {
    stores.useNormalImportStore.getState().patch({
      result: {
        job_id: 'job-1',
        documents: 1,
        records: 2,
        entities: 3,
        failed: 0,
        errors: [],
      },
    });
    const state = stores.useNormalImportStore.getState();
    assert.equal(state.result?.records, 2);
    assert.equal((persisted(NORMAL_KEY).result as { entities: number }).entities, 3);
  });
});
