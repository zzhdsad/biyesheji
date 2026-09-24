/**
 * BUG-059：窗口渲染边界测试。
 *
 * 覆盖：负/零/越界/小数步长、数据被删后窗口自动收敛、"加载更多"不会越过总数。
 *
 * 运行：npm test（node --test）
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { KB_WINDOW_SIZE, clampVisibleCount, nextVisibleCount } from './pagination.ts';

describe('窗口渲染可见数量', () => {
  it('首屏常量为正整数，避免误配成 0 导致白屏', () => {
    assert.ok(Number.isInteger(KB_WINDOW_SIZE));
    assert.ok(KB_WINDOW_SIZE > 0);
  });

  it('可见数量超过总数时收敛到总数', () => {
    assert.equal(clampVisibleCount(5, 100), 5);
    assert.equal(clampVisibleCount(0, 24), 0);
  });

  it('负数或非数字按 0 处理，不抛异常', () => {
    assert.equal(clampVisibleCount(10, -1), 0);
    assert.equal(clampVisibleCount(10, Number.NaN), 0);
    assert.equal(clampVisibleCount(10, Number.POSITIVE_INFINITY), 10);
  });

  it('小数向下取整，slice 行为保持确定', () => {
    assert.equal(clampVisibleCount(10, 3.9), 3);
  });

  it('加载更多按步长递增且不越过总数', () => {
    const total = 60;
    let visible = clampVisibleCount(total, KB_WINDOW_SIZE);
    assert.equal(visible, KB_WINDOW_SIZE);

    visible = nextVisibleCount(total, visible);
    assert.equal(visible, KB_WINDOW_SIZE * 2);

    visible = nextVisibleCount(total, visible);
    assert.equal(visible, total, '到达总数后必须收敛，不得越过');

    visible = nextVisibleCount(total, visible);
    assert.equal(visible, total, '已到末尾再点加载更多应保持 total');
  });

  it('数据被删后（total 变小）旧窗口值自动收敛', () => {
    assert.equal(clampVisibleCount(3, 48), 3);
  });

  it('非法步长按 0 处理，窗口不变小也不变负', () => {
    assert.equal(nextVisibleCount(50, 24, 0), 24);
    assert.equal(nextVisibleCount(50, 24, -12), 24);
  });
});
