/**
 * 文档格式能力表守卫：前端展示与上传白名单必须与后端保持一致。
 *
 * 后端 src/core/document_formats.py 是唯一事实来源；本测试只读源码，不发请求。
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { describe, it } from 'node:test';

import {
  DOCUMENT_FORMATS,
  EDITABLE_FORMATS,
  IMAGE_FORMATS,
  UPLOAD_ACCEPT,
  UPLOAD_HINT,
  acceptFor,
  isEditableFormat,
} from './documentFormats.ts';

const REPO_ROOT = path.resolve(process.cwd(), '..');
const BACKEND_FORMATS = fs.readFileSync(
  path.resolve(REPO_ROOT, 'backend/src/core/document_formats.py'),
  'utf8',
);

/** 提取 `NAME = frozenset({"a", "b"})` 中的扩展名集合。 */
function backendExtSet(name: string): Set<string> {
  const match = BACKEND_FORMATS.match(new RegExp(`${name} = frozenset\\(\\{([^}]*)\\}\\)`));
  assert.ok(match, `后端未声明 ${name}`);
  return new Set(
    match[1]
      .split(',')
      .map((token) => token.trim().replace(/^"|"$/g, ''))
      .filter(Boolean),
  );
}

/** 展开 `A | B | C` 形式的集合并（当前用于 ALLOWED_EXTENSIONS）。 */
function backendAllowedSet(): Set<string> {
  const match = BACKEND_FORMATS.match(/ALLOWED_EXTENSIONS = (.+)/);
  assert.ok(match, '后端未声明 ALLOWED_EXTENSIONS');
  const out = new Set<string>();
  for (const name of match[1].split('|').map((s) => s.trim())) {
    for (const ext of Array.from(backendExtSet(name))) out.add(ext);
  }
  return out;
}

describe('文档格式能力表', () => {
  it('格式说明表恰好覆盖任务要求的 10 种格式', () => {
    assert.deepEqual(
      [...DOCUMENT_FORMATS.map((f) => f.ext)].sort(),
      ['csv', 'docx', 'html', 'json', 'md', 'pdf', 'pptx', 'tsv', 'txt', 'xlsx'],
    );
    assert.equal(DOCUMENT_FORMATS.length, 10);
  });

  it('在线编辑集合与后端 TEXT_EXTENSIONS 一致', () => {
    assert.deepEqual(
      Array.from(EDITABLE_FORMATS).sort(),
      Array.from(backendExtSet('TEXT_EXTENSIONS')).sort(),
    );
    for (const row of DOCUMENT_FORMATS) {
      assert.equal(row.editable, isEditableFormat(row.ext), `${row.ext} 编辑标记不一致`);
      assert.equal(row.editable, EDITABLE_FORMATS.has(row.ext));
    }
  });

  it('上传 accept 与后端白名单完全一致（含图片，不含未支持格式）', () => {
    const accepted = UPLOAD_ACCEPT.split(',').map((s) => s.replace(/^\./, ''));
    assert.deepEqual(
      accepted.slice().sort(),
      Array.from(backendAllowedSet()).sort(),
      '前端 accept 与后端 ALLOWED_EXTENSIONS 漂移',
    );
    assert.deepEqual(
      Array.from(IMAGE_FORMATS).sort(),
      Array.from(backendExtSet('IMAGE_EXTENSIONS')).sort(),
    );
  });

  it('表格类格式的编辑方式标签与后端 TABLE_EXTENSIONS 一致', () => {
    const tableExts = backendExtSet('TABLE_EXTENSIONS');
    for (const row of DOCUMENT_FORMATS) {
      if (tableExts.has(row.ext)) assert.equal(row.editLabel, '表格编辑');
      else if (row.editable) assert.ok(row.editLabel, `${row.ext} 可编辑但缺少标签`);
      else assert.equal(row.editLabel, '');
    }
  });

  it('acceptFor 限制到单个扩展名，UPLOAD_HINT 列出全部文档格式', () => {
    assert.equal(acceptFor('PDF'), '.pdf');
    assert.equal(acceptFor('docx'), '.docx');
    assert.equal(
      UPLOAD_HINT,
      DOCUMENT_FORMATS.map((f) => f.label).join(' / '),
    );
    for (const label of ['DOCX', 'PDF', 'MD', 'TXT', 'CSV', 'TSV', 'XLSX', 'JSON', 'HTML', 'PPTX']) {
      assert.ok(UPLOAD_HINT.includes(label), `上传提示缺少 ${label}`);
    }
  });
});
