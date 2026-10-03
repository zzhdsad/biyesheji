/**
 * 证据原文翻译能力测试（问题4/5）。
 *
 * 运行：npm test（node --test，零新增测试依赖）
 *
 * 覆盖：
 * - needsTranslation：中文原文 / 英文原文 / 中英混合（英文为主）/ 少量英文术语 / 空文本；
 * - getEvidenceTranslation：中文原文不请求、翻译成功、翻译失败 fallback、
 *   已缓存直接命中（不重复请求）。
 */
import assert from 'node:assert/strict';
import { beforeEach, describe, it } from 'node:test';

import {
  TRANSLATION_FALLBACK,
  clearTranslationCache,
  getEvidenceTranslation,
  needsTranslation,
} from './translation';

describe('needsTranslation（是否提供中文翻译）', () => {
  it('纯中文原文：不需要翻译', () => {
    assert.equal(needsTranslation('太阳中风，阳浮而阴弱，啬啬恶寒，淅淅恶风。'), false);
  });

  it('少量英文术语的中文原文：不整段翻译（避免翻译成奇怪的中文）', () => {
    assert.equal(needsTranslation('丹参具有活血化瘀的功效，其有效成分包括 Tanshinone IIA 等。'), false);
  });

  it('英文为主的原文：需要翻译', () => {
    assert.equal(
      needsTranslation('Guizhi Decoction is used for exterior deficiency syndrome.'),
      true,
    );
  });

  it('中英混合且英文占比高：需要翻译', () => {
    assert.equal(
      needsTranslation('The syndrome of Taiyang disease involves 恶寒 发热 headache.'),
      true,
    );
  });

  it('空文本 / 纯数字符号：不需要翻译', () => {
    assert.equal(needsTranslation(''), false);
    assert.equal(needsTranslation('123 · ……'), false);
    assert.equal(needsTranslation(null), false);
  });
});

describe('getEvidenceTranslation（懒加载 + 缓存）', () => {
  beforeEach(() => {
    clearTranslationCache();
  });

  it('中文原文：直接返回 isChinese，不发请求', async () => {
    let called = 0;
    const res = await getEvidenceTranslation('桂枝汤适用于太阳中风证。', async () => {
      called += 1;
      return { translation: '不应到达', is_chinese: false };
    });
    assert.equal(res.isChinese, true);
    assert.equal(res.translation, null);
    assert.equal(called, 0, '中文原文不得触发翻译请求');
  });

  it('英文原文：请求翻译并缓存，再次获取不重复请求', async () => {
    let called = 0;
    const fetcher = async (text: string) => {
      called += 1;
      assert.equal(text, 'Guizhi Decoction');
      return { translation: '桂枝汤', is_chinese: false };
    };
    const first = await getEvidenceTranslation('Guizhi Decoction', fetcher);
    assert.equal(first.translation, '桂枝汤');
    assert.equal(first.isChinese, false);

    // 第二次（已缓存）：不再调用 fetcher
    const second = await getEvidenceTranslation('Guizhi Decoction', fetcher);
    assert.equal(second.translation, '桂枝汤');
    assert.equal(called, 1, '缓存命中后不得重复请求');
  });

  it('翻译失败（后端异常）：返回统一 fallback 文案，不抛出', async () => {
    const res = await getEvidenceTranslation('English text only', async () => {
      throw new Error('503');
    });
    assert.equal(res.error, TRANSLATION_FALLBACK);
    assert.equal(res.translation, null);
  });

  it('后端返回空译文：视为失败 fallback，不缓存空串', async () => {
    const res = await getEvidenceTranslation('English text only', async () => ({
      translation: '  ',
      is_chinese: false,
    }));
    assert.equal(res.error, TRANSLATION_FALLBACK);
  });

  it('后端判定原文为中文（is_chinese=true）：不发缓存，不返回译文', async () => {
    const res = await getEvidenceTranslation('Mixed text 术语较多吗', async () => ({
      translation: null,
      is_chinese: true,
    }));
    assert.equal(res.isChinese, true);
    assert.equal(res.translation, null);
  });
});
