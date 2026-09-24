/**
 * BUG-048：请求序号守卫测试（列表页 / 会话切换的"慢响应覆盖新响应"防护）。
 *
 * 运行：npm test（node --test）
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { createRequestSeq } from './requestSeq.ts';

describe('createRequestSeq', () => {
  it('begin 递增并发号，最新一次 isLatest 为真', () => {
    const seq = createRequestSeq();
    const first = seq.begin();
    assert.equal(first, 1);
    assert.equal(seq.isLatest(first), true);

    const second = seq.begin();
    assert.equal(second, 2);
    assert.equal(seq.isLatest(first), false, '旧请求必须作废');
    assert.equal(seq.isLatest(second), true);
  });

  it('invalidate 作废所有在飞请求（切换视图 / 删除资源场景）', () => {
    const seq = createRequestSeq();
    const inflight = seq.begin();
    assert.equal(seq.isLatest(inflight), true);

    seq.invalidate();
    assert.equal(seq.isLatest(inflight), false, 'invalidate 后旧响应不得写入状态');
  });

  it('current 返回当前序号，供"发起时快照"的回调校验归属', () => {
    const seq = createRequestSeq();
    const id = seq.begin();
    assert.equal(seq.current(), id);

    seq.invalidate();
    assert.equal(seq.isLatest(id), false);
    assert.equal(seq.current(), id + 1);
  });

  it('多次 invalidate 不会回退，且后续请求仍可正常作废', () => {
    const seq = createRequestSeq();
    const a = seq.begin();
    seq.invalidate();
    seq.invalidate();
    const b = seq.begin();
    assert.equal(seq.isLatest(a), false);
    assert.equal(seq.isLatest(b), true);
    seq.invalidate();
    assert.equal(seq.isLatest(b), false);
  });
});
