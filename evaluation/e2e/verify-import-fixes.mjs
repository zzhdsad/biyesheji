/**
 * 一次性浏览器验证：同名文件阻断 + 导入失败提示。
 *
 * 仅操作隔离开发环境，只读/不写入业务数据（失败场景用路由拦截）。
 * 运行（cwd = evaluation/e2e）：node verify-import-fixes.mjs
 */
import { chromium } from 'playwright';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = path.join(__dirname, 'screens');
const BASE = 'http://localhost:3000';
const EMAIL = 'e2e@biyesheji.dev';
const PASSWORD = 'E2ePass123!';
const CSV_TERMS = '术语id,术语名称,来源\nT1,脾胃虚弱,神农本草经\nT2,肝郁气滞,伤寒论\n';
const CSV_MEDICINES = '中成药id,名称\nM1,四君子汤\nM2,逍遥散\n';

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok });
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) process.exitCode = 1;
}

function payload(name, content) {
  return { name, mimeType: 'text/csv', buffer: Buffer.from(content) };
}

let browser;
let context;
const suiteTimer = setTimeout(() => {
  console.error('[TIMEOUT] 浏览器验证超时，强制结束');
  process.exitCode = 1;
  void context?.close().catch(() => {});
  void browser?.close().catch(() => {});
}, 150000);

try {
  browser = await chromium.launch();
  context = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
  const page = await context.newPage();
  page.setDefaultTimeout(20000);

  await page.goto(`${BASE}/login`);
  await page.fill('input#username_or_email', EMAIL);
  await page.fill('input#password', PASSWORD);
  await page.click('button[type=submit]');
  await page.waitForURL('**/chat', { timeout: 60000 });

  const token = await page.evaluate(() => window.localStorage.getItem('kp_token'));
  const auth = { Authorization: `Bearer ${token}` };
  const kbResp = await page.request.post(`${BASE}/api/v1/kb`, {
    data: { name: `验证同名文件-${Date.now()}` },
    headers: auth,
  });
  if (!kbResp.ok()) throw new Error(`创建知识库失败 ${kbResp.status()}`);
  const kbId = (await kbResp.json()).id;

  // ── 场景 1：不同目录（路径无法区分）的同名 CSV ──────────────────────────
  await page.goto(`${BASE}/admin/import/relational`);
  const fileInput = page.locator('input[type=file]').first();
  await fileInput.setInputFiles([
    payload('同名.csv', CSV_TERMS),
    payload('同名.csv', CSV_MEDICINES),
  ]);
  await page.waitForSelector('text=存在无法区分的同名文件', { timeout: 10000 });
  check('同名文件在选择区给出明确警告', true);

  const [analyzeResp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/api/v1/import/relational/analyze')),
    page.locator('button:has-text("分析结构")').click(),
  ]);
  check('同名文件分析返回 400', analyzeResp.status() === 400, `HTTP ${analyzeResp.status()}`);
  const analyzeToast = page.locator('.ant-message-notice-content', {
    hasText: '重复的上传路径',
  });
  await analyzeToast.waitFor({ timeout: 10000 });
  const analyzeText = await analyzeToast.innerText();
  check('同名文件错误原因展示到前端', analyzeText.includes('重复的上传路径'), analyzeText);
  check('同名文件未显示分析成功', !(await page.locator('.ant-message-notice-content', {
    hasText: '分析完成',
  }).count()));
  await page.screenshot({ path: path.join(SHOTS, '12-duplicate-file-error.png') });

  // ── 场景 2：提交返回 4xx 时必须提示失败，而不是“导入完成” ───────────────
  await page.locator('button:has-text("清空")').click();
  await fileInput.setInputFiles([
    payload('D1.csv', CSV_TERMS),
    payload('D2.csv', CSV_MEDICINES),
  ]);
  await page.locator('[data-testid="kb-select"] .ant-select').click();
  const dropdown = page.locator('.ant-select-dropdown:visible').last();
  const ourKb = dropdown.locator('.ant-select-item-option').filter({ hasText: '验证同名文件-' }).last();
  await ourKb.waitFor();
  await ourKb.click();

  const commitRe = /\/api\/v1\/import\/relational\/commit$/;
  await page.route(commitRe, async (route) => {
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '会话不存在或已过期，请重新上传文件' }),
    });
  });

  const [analyzeOk] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/api/v1/import/relational/analyze')),
    page.locator('button:has-text("分析结构")').click(),
  ]);
  check('有效文件分析成功', analyzeOk.status() === 200, `HTTP ${analyzeOk.status()}`);
  await page.waitForSelector('text=D1', { timeout: 20000 });

  const [commitResp] = await Promise.all([
    page.waitForResponse((r) => commitRe.test(r.url())),
    page.locator('button:has-text("开始导入")').click(),
  ]);
  check('提交失败返回 4xx', commitResp.status() === 400, `HTTP ${commitResp.status()}`);
  const commitToast = page.locator('.ant-message-notice-content', {
    hasText: '会话不存在或已过期',
  });
  await commitToast.waitFor({ timeout: 10000 });
  const commitText = await commitToast.innerText();
  check('提交失败原因展示到前端', commitText.includes('会话不存在或已过期'), commitText);
  await page.waitForTimeout(300);
  const successToast = await page.locator('.ant-message-notice-content', {
    hasText: '导入完成',
  }).count();
  check('提交失败时没有“导入完成”提示', successToast === 0, `count=${successToast}`);
  await page.screenshot({ path: path.join(SHOTS, '13-import-failure-error.png') });

  await page.unroute(commitRe);

  // ── 场景 3：普通导入成功路径仍应提示成功 ─────────────────────────────────
  await page.goto(`${BASE}/admin/import/normal`);
  await page.locator('.ant-select').first().click();
  const normalDropdown = page.locator('.ant-select-dropdown:visible').last();
  const normalKb = normalDropdown
    .locator('.ant-select-item-option')
    .filter({ hasText: '验证同名文件-' })
    .last();
  await normalKb.waitFor();
  await normalKb.click();

  await page.locator('input[type=file]').first().setInputFiles([
    payload('N1.csv', CSV_TERMS),
    payload('N2.csv', CSV_MEDICINES),
  ]);
  await page.locator('button:has-text("扫描文件")').click();
  await page.locator('.ant-message-notice-content', { hasText: '扫描完成' }).waitFor({
    timeout: 20000,
  });
  check('普通导入扫描成功', true);

  const [normalCommit] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/api/v1/import/general/commit')),
    page.locator('button:has-text("开始导入")').click(),
  ]);
  check('普通导入提交返回 200', normalCommit.status() === 200, `HTTP ${normalCommit.status()}`);
  const success = page.locator('.ant-message-notice-content', { hasText: '导入完成：文档' });
  await success.waitFor({ timeout: 20000 });
  check('普通导入成功仍提示成功', (await success.innerText()).includes('失败 0'));
  check(
    '普通导入成功未出现失败提示',
    (await page.locator('.ant-message-notice-content', { hasText: '导入失败' }).count()) === 0,
  );
  await page.screenshot({ path: path.join(SHOTS, '14-normal-import-success.png') });
} catch (error) {
  process.exitCode = 1;
  console.error('[ERROR]', error?.stack || error);
  check('浏览器验证无异常', false, error?.message || String(error));
} finally {
  clearTimeout(suiteTimer);
  await context?.close().catch(() => {});
  await browser?.close().catch(() => {});
  const passed = results.filter((r) => r.ok).length;
  console.log(`\n结果：${passed}/${results.length} 通过`);
}
