/**
 * BUG-061：token 存储契约测试。
 *
 * 锁定两条不变量：
 * 1. cookie 有效期必须是 24 小时（= 后端 ACCESS_TOKEN_EXPIRE_MINUTES 1440 分钟），
 *    既不能回到写死的 7 天，也不得比 JWT 更长（否则 middleware 会放行已过期会话）；
 * 2. setToken 真的把该有效期写进 document.cookie，且与 localStorage 双写保持一致。
 *
 * 用最小 fake 的 window/document 替代 jsdom，保持 node --test 零依赖。
 *
 * 运行：npm test（node --test）
 */
import assert from 'node:assert/strict';
import { describe, it, before } from 'node:test';

/** 后端 `ACCESS_TOKEN_EXPIRE_MINUTES` 的当前值，改动后端 TTL 时需同步更新此处。 */
const BACKEND_JWT_EXPIRE_MINUTES = 1440;

interface FakeGlobals {
  writtenCookies: string[];
  localStorage: Map<string, string>;
}

function installFakeBrowser(): FakeGlobals {
  const writtenCookies: string[] = [];
  const localStorage = new Map<string, string>();

  const fakeWindow = {
    localStorage: {
      getItem: (key: string) => localStorage.get(key) ?? null,
      setItem: (key: string, value: string) => void localStorage.set(key, value),
      removeItem: (key: string) => void localStorage.delete(key),
    },
    location: { pathname: '/kb', search: '' },
  };

  const fakeDocument = {};
  Object.defineProperty(fakeDocument, 'cookie', {
    set: (value: string) => void writtenCookies.push(value),
    get: () => writtenCookies.join('; '),
    configurable: true,
  });

  (globalThis as unknown as { window: unknown }).window = fakeWindow;
  (globalThis as unknown as { document: unknown }).document = fakeDocument;

  return { writtenCookies, localStorage };
}

describe('token 存储（BUG-061：cookie 与 JWT 有效期对齐）', () => {
  let fake: FakeGlobals;
  let token: typeof import('./token.ts');

  before(async () => {
    fake = installFakeBrowser();
    token = await import('./token.ts');
  });

  it('cookie 有效期等于后端 JWT 有效期（24 小时），不小于也不超过', () => {
    const seconds = token.TOKEN_MAX_AGE_SECONDS;
    assert.equal(seconds, BACKEND_JWT_EXPIRE_MINUTES * 60);
    assert.equal(seconds, 86400);
    assert.ok(seconds <= BACKEND_JWT_EXPIRE_MINUTES * 60, 'cookie 不得比 JWT 活得更久');
  });

  it('setToken 把该有效期写入 cookie，并同步写入 localStorage', () => {
    fake.writtenCookies.length = 0;
    token.setToken('dummy-token-value');

    assert.equal(token.getToken(), 'dummy-token-value', 'localStorage 必须写入');
    const cookie = fake.writtenCookies.at(-1) ?? '';
    assert.ok(cookie.includes('kp_token=dummy-token-value'), `cookie 内容异常：${cookie}`);
    assert.ok(
      cookie.includes(`max-age=${token.TOKEN_MAX_AGE_SECONDS}`),
      `cookie max-age 与 TOKEN_MAX_AGE_SECONDS 不一致：${cookie}`,
    );
    assert.ok(cookie.includes('SameSite=Lax'));
    assert.ok(cookie.includes('path=/'));
  });

  it('clearToken 立即使 cookie 失效（max-age=0）', () => {
    fake.writtenCookies.length = 0;
    token.clearToken();

    const cookie = fake.writtenCookies.at(-1) ?? '';
    assert.ok(cookie.includes('max-age=0'), `清退 cookie 必须带 max-age=0：${cookie}`);
    assert.equal(token.getToken(), null);
  });
});
