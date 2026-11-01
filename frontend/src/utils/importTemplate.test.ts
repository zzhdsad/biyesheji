/**
 * 映射模板前端工具测试：字段差异口径必须与后端 _field_diff 一致。
 *
 * 运行：npm test（node --test）
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import type { ImportTemplateConfig } from '@/services/api';
import {
  buildTemplateConfig,
  canApplyTemplate,
  diffTemplate,
  tablesFieldMap,
  templateDependencyFields,
  templatePrimaryKeys,
} from './importTemplate.ts';

const CONFIG: ImportTemplateConfig = {
  version: 2,
  tables: {
    D1术语: { table: 'D1术语', entity_type: '术语', pk_field: '术语id', display_field: null },
    D2中成药: { table: 'D2中成药', entity_type: '中成药', pk_field: '中成药id', display_field: null },
  },
  relations: [
    {
      child_table: 'D3关联',
      child_field: '中成药id',
      parent_table: 'D2中成药',
      parent_field: '中成药id',
      relation_type: 'contains',
    },
  ],
  junctions: [],
};

describe('映射模板字段差异', () => {
  it('字段完全覆盖时判定 exact 且可用', () => {
    const diff = diffTemplate(CONFIG, {
      D1术语: ['术语id', '术语名称'],
      D2中成药: ['中成药id', '名称'],
      D3关联: ['中成药id', '术语id'],
    });
    assert.equal(diff.status, 'exact');
    assert.equal(diff.usable, true);
    assert.deepEqual(diff.missing_fields, {});
    assert.deepEqual(diff.extra_fields, {});
  });

  it('仅新增模板未覆盖的表时判定 compatible（可用但需确认）', () => {
    const diff = diffTemplate(CONFIG, {
      D1术语: ['术语id', '术语名称'],
      D2中成药: ['中成药id'],
      D3关联: ['中成药id', '术语id'],
      D4新增: ['id'],
    });
    assert.equal(diff.status, 'compatible');
    assert.equal(diff.usable, true);
    assert.deepEqual(diff.extra_fields, { D4新增: ['id'] });
  });

  it('模板依赖字段缺失时判定 mismatch 且禁止套用', () => {
    const diff = diffTemplate(CONFIG, {
      D1术语: ['术语名称'],
      D2中成药: ['中成药id'],
      D3关联: ['中成药id', '术语id'],
    });
    assert.equal(diff.status, 'mismatch');
    assert.equal(diff.usable, false);
    assert.deepEqual(diff.missing_fields, { D1术语: ['术语id'] });
    assert.equal(canApplyTemplate('mismatch', diff), false);
  });

  it('整张表缺失时给出明确标记，而不是静默通过', () => {
    const diff = diffTemplate(CONFIG, {
      D1术语: ['术语id'],
      D3关联: ['中成药id', '术语id'],
    });
    assert.equal(diff.status, 'mismatch');
    assert.deepEqual(diff.missing_fields, { D2中成药: ['<整张表缺失>'] });
  });

  it('关系表字段缺失同样拦截', () => {
    const diff = diffTemplate(CONFIG, {
      D1术语: ['术语id'],
      D2中成药: ['中成药id'],
      D3关联: ['术语id'],
    });
    assert.equal(diff.status, 'mismatch');
    assert.deepEqual(diff.missing_fields, { D3关联: ['中成药id'] });
  });

  it('字段比较不区分大小写（与后端一致）', () => {
    const diff = diffTemplate(CONFIG, {
      D1术语: ['术语ID', '术语名称'],
      D2中成药: ['中成药ID'],
      D3关联: ['中成药ID', '术语ID'],
    });
    assert.equal(diff.usable, true);
  });

  it('不兼容/缺失模板不允许套用', () => {
    const usable = diffTemplate(CONFIG, {
      D1术语: ['术语id'],
      D2中成药: ['中成药id'],
      D3关联: ['中成药id', '术语id'],
    });
    assert.equal(canApplyTemplate('incompatible', usable), false);
    assert.equal(canApplyTemplate('mismatch', { ...usable, usable: false }), false);
    assert.equal(canApplyTemplate('none', usable), false);
    assert.equal(canApplyTemplate('exact', usable), true);
    assert.equal(canApplyTemplate('compatible', usable), true);
  });
});

describe('模板配置构建', () => {
  it('由页面选择生成 version 2 配置', () => {
    const config = buildTemplateConfig({
      tables: ['D1术语', 'D2中成药'],
      primaryKeys: { D1术语: '术语id' },
      foreignKeys: [
        {
          child_table: 'D3关联',
          child_field: '中成药id',
          parent_table: 'D2中成药',
          parent_field: '中成药id',
        },
      ],
      junctions: [],
    });
    assert.equal(config.version, 2);
    assert.equal(config.tables['D1术语'].pk_field, '术语id');
    assert.equal(config.tables['D2中成药'].pk_field, null);
    assert.equal(config.relations[0].relation_type, 'related_to');
    assert.deepEqual(templatePrimaryKeys(config), { D1术语: '术语id' });
  });

  it('列出模板依赖字段供详情展示', () => {
    const fields = templateDependencyFields(CONFIG);
    assert.deepEqual(
      fields.filter((row) => row.kind === '主键').map((row) => `${row.table}.${row.field}`),
      ['D1术语.术语id', 'D2中成药.中成药id'],
    );
    assert.ok(
      fields.some(
        (row) => row.table === 'D3关联' && row.field === '中成药id' && row.kind === '外键',
      ),
    );
  });

  it('tablesFieldMap 保留分析结果中的字段名', () => {
    assert.deepEqual(
      tablesFieldMap([
        { table: 'D1', fields: [{ name: 'a' }, { name: 'b' }] },
        { table: 'D2', fields: [] },
      ]),
      { D1: ['a', 'b'], D2: [] },
    );
  });
});
