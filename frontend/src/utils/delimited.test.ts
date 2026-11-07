/**
 * CSV/TSV 序列化测试：覆盖分隔符、引号、换行与不齐整行的转义。
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { normalizeTableRows, serializeDelimited } from './delimited.ts';

describe('表格序列化', () => {
  it('普通单元格按分隔符拼接', () => {
    assert.equal(
      serializeDelimited(
        [
          ['姓名', '部门'],
          ['张三', '研发'],
        ],
        ',',
      ),
      '姓名,部门\n张三,研发',
    );
  });

  it('含逗号/引号/换行的单元格正确转义', () => {
    assert.equal(
      serializeDelimited([['a,b', '他说"好"', '第一行\n第二行']], ','),
      '"a,b","他说""好""","第一行\n第二行"',
    );
  });

  it('TSV 按制表符拼接并转义制表符', () => {
    assert.equal(serializeDelimited([['a\tb', 'c']], '\t'), '"a\tb"\tc');
  });

  it('不齐整行补齐为矩形，避免表格错位', () => {
    assert.deepEqual(normalizeTableRows([['a'], ['b', 'c', 'd']]), [
      ['a', '', ''],
      ['b', 'c', 'd'],
    ]);
  });
});
