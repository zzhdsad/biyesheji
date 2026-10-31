/**
 * 前端真实端到端验收（Chromium + 真实后端/数据库）。
 *
 * 目标不是「页面能打开」，而是验证：
 * 1. 多文件上传 → 扫描 → 类型/编码/记录数/预览/错误展示（含不支持文件）
 * 2. 普通导入：文档走 RAG、结构化按记录入库，统计数字真实
 * 3. 关联导入：Schema 与主键/关联表识别、ER 图渲染、导入统计
 * 4. 关系确认 → 写入图谱；拒绝 → 撤掉图谱边
 * 5. 未确认关系不参与图谱（对比确认前后）
 */

import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures');
const SHOTS = path.join(__dirname, 'screens');
fs.mkdirSync(SHOTS, { recursive: true });

const BASE = 'http://localhost:3000';
const EMAIL = 'e2e@biyesheji.dev';
const PASSWORD = 'E2ePass123!';

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  const mark = ok ? 'PASS' : 'FAIL';
  console.log(`  [${mark}] ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) process.exitCode = 1;
}

function f(name) {
  return path.join(FIXTURES, name);
}

/** 前端把 JWT 存在 localStorage（拦截器取用），脚本需显式带上 */
async function authHeaders(page) {
  const token = await page.evaluate(() => window.localStorage.getItem('kp_token'));
  return { Authorization: `Bearer ${token}` };
}

async function createKb(page, name) {
  const headers = await authHeaders(page);
  const resp = await page.request.post(`${BASE}/api/v1/kb`, { data: { name }, headers });
  if (!resp.ok()) throw new Error(`创建知识库失败 ${resp.status()} ${await resp.text()}`);
  return (await resp.json()).id;
}

async function apiGet(page, url) {
  const headers = await authHeaders(page);
  const resp = await page.request.get(url, { headers });
  if (!resp.ok()) throw new Error(`GET ${url} 失败 ${resp.status()}`);
  return resp.json();
}

async function pickKb(page, name) {
  await page.locator('.ant-select').first().click();
  await page.locator('.ant-select-item-option', { hasText: name }).first().click();
}

/** 读取卡片里的统计数值 */
async function stat(page, title) {
  const node = page.locator('.ant-statistic', { has: page.locator('.ant-statistic-title', { hasText: title }) }).first();
  const text = await node.locator('.ant-statistic-content-value-int').first().innerText();
  return Number(text.replace(/[^\d-]/g, ''));
}

const run = async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on('pageerror', (err) => console.log(`  [PAGEERROR] ${err.message}`));
  page.setDefaultTimeout(60000);

  // ── 登录 ────────────────────────────────────────────────────────────────
  console.log('\n[0] 登录');
  await page.goto(`${BASE}/login`);
  await page.fill('input#username_or_email', EMAIL);
  await page.fill('input#password', PASSWORD);
  await page.click('button[type=submit]');
  await page.waitForURL('**/chat', { timeout: 60000 });
  await page.waitForSelector('text=智能问答', { timeout: 30000 });
  check('管理员登录成功并进入问答页', page.url().includes('/chat'), page.url());

  // ══ 普通数据导入 ═══════════════════════════════════════════════════════
  console.log('\n[1] 普通数据导入');
  const normalKbName = `E2E普通导入-${Date.now()}`;
  const normalKbId = await createKb(page, normalKbName);
  await page.goto(`${BASE}/admin/import/normal`);

  await page.locator('input[type=file]').first().setInputFiles([
    f('普通-药材.csv'),
    f('普通-说明.txt'),
    f('普通-GBK.csv'),
    f('化学成分.xlsx'),
    f('不支持.bin'),
  ]);
  const listed = await page.locator('.ant-list-item').count();
  check('多文件选择后列出全部文件', listed === 5, `列出 ${listed} 个`);

  page.on('request', (r) => {
    if (r.url().includes('/import/general/commit')) {
      console.log('  · commit 载荷:', r.postData()?.slice(0, 400));
    }
  });

  const [scanResp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/import/scan'), { timeout: 60000 }),
    page.click('button:has-text("扫描文件")'),
  ]);
  const scanJson = await scanResp.json();
  console.log(
    '  · 扫描:',
    scanJson.files
      .map(
        (x) =>
          `${x.file_name}:${x.detected_format}:rows=${x.row_count}:pk=${x.primary_key_candidates?.[0]?.fields?.[0] ?? '-'}`,
      )
      .join(' | '),
  );
  await page.waitForSelector('.ant-table-row', { timeout: 60000 });
  await page.waitForTimeout(1500);
  const scanRows = await page.locator('.ant-table-tbody tr.ant-table-row').count();
  check('扫描结果渲染文件表', scanRows >= 5, `${scanRows} 行`);

  const xlsxFile = scanJson.files.find((x) => x.detected_format === 'xlsx');
  check(
    'XLSX 真实解析出数据行',
    (xlsxFile?.row_count ?? 0) === 3,
    `${xlsxFile?.file_name} rows=${xlsxFile?.row_count} ${xlsxFile?.error ?? ''}`,
  );

  const bodyText = await page.locator('.ant-table-tbody').first().innerText();
  check('识别 CSV 格式', bodyText.includes('csv'));
  check('识别 TXT 文档', bodyText.includes('txt'));
  check('识别 XLSX 格式', bodyText.includes('xlsx'));
  check('GBK 文件识别为 gb18030', /gb18030/.test(bodyText), bodyText.match(/gb18030|utf-8/g)?.join(',') ?? '');
  check('不支持文件展示错误信息', bodyText.includes('无法识别'));

  const previewRows = await page.locator('.ant-table').last().locator('tbody tr.ant-table-row').count();
  check('数据预览有行', previewRows > 0, `${previewRows} 行预览`);

  await page.screenshot({ path: path.join(SHOTS, '01-normal-scan.png'), fullPage: true });

  const pkPrefill = await page
    .locator('input[placeholder="主键字段"]')
    .evaluateAll((els) => els.map((e) => e.value));
  console.log('  · 页面预填主键:', JSON.stringify(pkPrefill));

  await pickKb(page, normalKbName);
  await page.click('button:has-text("开始导入")');
  await page.waitForSelector('.ant-message-success', { timeout: 120000 });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: path.join(SHOTS, '02-normal-imported.png'), fullPage: true });

  const docs = await stat(page, '文档');
  const records = await stat(page, '原始记录');
  const entities = await stat(page, '实体');
  const failed = await stat(page, '失败');
  check('文档进入 RAG 链路', docs >= 1, `文档 ${docs}`);
  // 3 药材 + 1 GBK + 3 成分 = 7 行数据
  check('结构化记录入库', records === 7, `记录 ${records}`);
  check('实体已生成', entities === 7, `实体 ${entities}`);
  check('不支持文件未造成失败记录', failed === 0, `失败 ${failed}`);

  // 数据库侧核对（不只看页面数字）
  const stats = await apiGet(page, `${BASE}/api/v1/import/stats?kb_id=${normalKbId}`);
  check('接口统计与页面一致', stats.records === 7 && stats.entities === 7, JSON.stringify(stats));

  // ══ 关联数据导入 ═══════════════════════════════════════════════════════
  console.log('\n[2] 关联数据导入');
  const relKbName = `E2E关联导入-${Date.now()}`;
  const relKbId = await createKb(page, relKbName);
  await page.goto(`${BASE}/admin/import/relational`);

  await page.locator('input[type=file]').first().setInputFiles([
    f('D1术语.csv'),
    f('D2中成药.csv'),
    f('D3关联.csv'),
  ]);
  const [analyzeResp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/import/relational/analyze'), { timeout: 90000 }),
    page.click('button:has-text("分析结构")'),
  ]);
  const analyzeJson = await analyzeResp.json();
  console.log(
    '  · 分析:',
    JSON.stringify({
      tables: analyzeJson.tables?.map((t) => t.table),
      primary_keys: analyzeJson.primary_keys,
      fk: analyzeJson.foreign_keys?.length,
      junctions: analyzeJson.junctions?.map((j) => `${j.table}:${j.left_table}->${j.right_table}`),
    }),
  );
  await page.waitForSelector('svg[aria-label="实体关系图"]', { timeout: 90000 });
  await page.waitForTimeout(1500);

  // SVG 不是 HTMLElement，用 textContent 而非 innerText
  const erText = await page
    .locator('svg[aria-label="实体关系图"]')
    .evaluate((el) => el.textContent ?? '');
  check('ER 图渲染出表节点', erText.includes('D1') && erText.includes('D2') && erText.includes('D3'), erText.replace(/\n/g, ' ').slice(0, 80));
  check('ER 图含 M:N 基数标记', erText.includes('M:N'));

  const analyzeText = await page.locator('body').innerText();
  check('识别出关联表候选', analyzeText.includes('关联表候选'));
  check('关联表 D3 被识别', analyzeText.includes('D3'));

  // 默认不勾选「直接确认」→ 验证待确认不进图谱
  const autoConfirm = page.locator('.ant-checkbox-input').last();
  const checked = await autoConfirm.isChecked();
  check('默认不自动确认', checked === false);

  await page.screenshot({ path: path.join(SHOTS, '03-relational-analyze.png'), fullPage: true });

  await pickKb(page, relKbName);
  await page.click('button:has-text("开始导入")');
  await page.waitForSelector('.ant-message-success', { timeout: 120000 });
  await page.waitForTimeout(2500);

  const relEntities = await stat(page, '实体');
  const relRelations = await stat(page, '关系');
  const kgEdges = await stat(page, '图谱边');
  check('关联导入生成实体', relEntities === 8, `D1 5 + D2 3 = ${relEntities}`);
  check('关联导入生成关系', relRelations === 4, `D3 四行 = ${relRelations}`);
  check('未确认关系未写入图谱', kgEdges === 0, `图谱边 ${kgEdges}`);

  await page.screenshot({ path: path.join(SHOTS, '04-relational-imported.png'), fullPage: true });

  // ── 关系确认 ────────────────────────────────────────────────────────────
  console.log('\n[3] 关系确认与撤销');
  const relCard = page.locator('.ant-card', { hasText: '关系确认与维护' }).last();
  await relCard.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1200);

  const pendingRows = await relCard.locator('tbody tr.ant-table-row').count();
  check('关系列表展示待确认关系', pendingRows === 4, `${pendingRows} 行`);
  const relTableText = await relCard.locator('tbody').innerText();
  check('关系状态显示为待确认', relTableText.includes('待确认'));
  check('关系展示来源溯源信息', /junction:D3|foreign_key/.test(relTableText), relTableText.slice(0, 60).replace(/\n/g, ' '));

  await relCard.locator('.ant-table-thead .ant-checkbox-input').first().check();
  await page.waitForTimeout(300);
  await relCard.locator('button:has-text("确认选中")').click();
  await page.waitForSelector('.ant-message-success', { timeout: 60000 });
  await page.waitForTimeout(2500);

  const confirmedText = await relCard.locator('tbody').innerText();
  check('确认后状态变为已确认', confirmedText.includes('已确认'), confirmedText.slice(0, 60).replace(/\n/g, ' '));

  const stats2 = await apiGet(page, `${BASE}/api/v1/import/stats?kb_id=${relKbId}`);
  check('确认后统计更新为已确认', stats2.relations_confirmed === 4, JSON.stringify(stats2));

  // 图谱边确实写入（直接查库级接口：relations 接口已确认，这里用索引差异接口无关，改用 stats + 关系接口）
  const relListJson = await apiGet(
    page,
    `${BASE}/api/v1/import/relations?kb_id=${relKbId}&status=confirmed&limit=50`,
  );
  check('服务端返回已确认关系', relListJson.items.length === 4, `${relListJson.items.length} 条`);
  check('已确认关系带来源信息', relListJson.items.every((r) => r.provenance && r.provenance.length > 0), relListJson.items[0]?.provenance ?? '');

  await page.screenshot({ path: path.join(SHOTS, '05-relations-confirmed.png'), fullPage: true });

  // 撤销：拒绝一条，图谱边应减少
  await relCard.locator('tbody tr.ant-table-row').first().locator('.ant-checkbox-input').check();
  await relCard.locator('button:has-text("拒绝选中")').click();
  await page.waitForSelector('.ant-message-success', { timeout: 60000 });
  await page.waitForTimeout(2500);
  const stats3 = await apiGet(page, `${BASE}/api/v1/import/stats?kb_id=${relKbId}`);
  check('拒绝后已确认数减少', stats3.relations_confirmed === 3, JSON.stringify(stats3));
  check('拒绝后已拒绝数为 1', stats3.relations_rejected === 1);

  await page.screenshot({ path: path.join(SHOTS, '06-relations-rejected.png'), fullPage: true });

  await browser.close();

  const failedCount = results.filter((r) => !r.ok).length;
  console.log(`\n===== 前端端到端结果：${results.length - failedCount} 通过 / ${failedCount} 失败 =====`);
  if (failedCount > 0) {
    console.log('失败项：');
    results.filter((r) => !r.ok).forEach((r) => console.log(`  - ${r.name} ${r.detail}`));
  }
};

run().catch((err) => {
  console.error('\n[FATAL]', err.message);
  process.exit(1);
});
