/**
 * 第三阶段真实浏览器验收：映射模板复用 + 索引一致性管理。
 *
 * 仅操作隔离开发环境（kp_dev / document_chunks_dev / uploads_dev）；
 * 检查只读，重建只重置状态，不删除任何向量。
 *
 * 运行（cwd = evaluation/e2e，Playwright 装在此目录）：
 *   node phase3.mjs                     # 全量
 *   node phase3.mjs --focus=template    # 仅映射模板（定向复现）
 *   node phase3.mjs --focus=index       # 仅索引一致性
 *   node run-supervised.mjs 240000 --focus=index  # 外部硬超时监督 + 进程树终止
 *
 * 日志实时输出到终端，同时完整写入 evaluation/e2e/phase3.log。
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures');
const SHOTS = path.join(__dirname, 'screens');
const LOG_PATH = path.join(__dirname, 'phase3.log');
fs.mkdirSync(SHOTS, { recursive: true });
fs.writeFileSync(LOG_PATH, '');

const BASE = 'http://localhost:3000';
const EMAIL = 'e2e@biyesheji.dev';
const PASSWORD = 'E2ePass123!';
const EXPECTED_COLLECTION = 'document_chunks_dev';

const FOCUS = (process.argv.find((arg) => arg.startsWith('--focus=')) || '--focus=all').split('=')[1];
const SUITE_TIMEOUT_MS = 8 * 60 * 1000;
const STEP_TIMEOUT_MS = 120 * 1000;

/** 匹配状态 → 页面标签（仅用于语义对应，不依赖后端提示文案）。 */
const TEMPLATE_STATUS_LABELS = {
  exact: '完全匹配',
  compatible: '可用（Schema 有新增）',
  mismatch: '不兼容：缺少字段',
  incompatible: '模板版本不可解释',
  none: '未匹配到模板',
};

const results = [];
const startedAt = Date.now();
function log(message = '') {
  const line = `[${((Date.now() - startedAt) / 1000).toFixed(1)}s] ${message}`;
  console.log(line);
  fs.appendFileSync(LOG_PATH, `${line}\n`);
}
function check(name, ok, detail = '') {
  results.push({ name, ok, detail });
  log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok) process.exitCode = 1;
}

/** 单步骤超时：超时后拒绝等待，避免整套测试无限挂起。 */
async function step(label, fn, timeoutMs = STEP_TIMEOUT_MS) {
  const started = Date.now();
  log(`▶ ${label}`);
  let timer;
  const work = Promise.resolve().then(fn);
  work.catch(() => {}); // 超时后原 promise 仍可能拒绝，避免未处理拒绝
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`步骤超时 ${timeoutMs}ms: ${label}`)), timeoutMs);
  });
  try {
    const out = await Promise.race([work, timeout]);
    log(`✔ ${label}（${((Date.now() - started) / 1000).toFixed(1)}s）`);
    return out;
  } catch (error) {
    log(`✘ ${label}（${((Date.now() - started) / 1000).toFixed(1)}s）：${error.message}`);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function waitUntil(predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待超时 ${timeoutMs}ms：${label}`);
}

const f = (name) => path.join(FIXTURES, name);
const fieldName = (optionText) => optionText.split('（')[0].trim();

async function authHeaders(page) {
  const token = await page.evaluate(() => window.localStorage.getItem('kp_token'));
  return { Authorization: `Bearer ${token}` };
}

async function createKb(page, name) {
  const resp = await page.request.post(`${BASE}/api/v1/kb`, {
    data: { name },
    headers: await authHeaders(page),
  });
  if (!resp.ok()) throw new Error(`创建知识库失败 ${resp.status()} ${await resp.text()}`);
  return (await resp.json()).id;
}

async function chooseOption(page, trigger, optionText) {
  await trigger.click();
  const dropdown = page.locator('.ant-select-dropdown:visible').last();
  await dropdown.waitFor();
  const option = dropdown.locator('.ant-select-item-option', { hasText: optionText }).first();
  await option.waitFor();
  await option.click();
}

/** 映射模板定向流程：首次匹配 → 另存 → 详情 → 套用 → 手动改映射 → 提交载荷核验。 */
async function templateFlow(page, kbName) {
  log('\n[1] 关联导入：自动匹配 / 查看 / 套用 / 另存新模板');
  await page.goto(`${BASE}/admin/import/relational`);
  const kbSelect = page.locator('[data-testid="kb-select"] .ant-select');
  await kbSelect.waitFor();
  await chooseOption(page, kbSelect, kbName);
  check('已选择目标知识库', (await kbSelect.innerText()).includes(kbName), await kbSelect.innerText());

  await page.locator('input[type=file]').first().setInputFiles([
    f('D1术语.csv'),
    f('D2中成药.csv'),
    f('D3关联.csv'),
  ]);

  const analyzeRespPromise = page.waitForResponse(
    (r) => r.url().includes('/import/relational/analyze'),
    { timeout: 120000 },
  );
  const matchRespPromise = page.waitForResponse(
    (r) => r.url().includes('/import/templates/match'),
    { timeout: 60000 },
  );
  await page.click('button:has-text("分析结构")');
  const analyzeResp = await analyzeRespPromise;
  check('关联分析返回 200', analyzeResp.status() === 200, `HTTP ${analyzeResp.status()}`);
  const analyzed = await analyzeResp.json();
  check('分析结果包含数据集指纹', Boolean(analyzed.dataset_fingerprint));
  const analyzedTables = (analyzed.tables || []).map((table) => table.table);
  log(`  [诊断] 分析表名=${JSON.stringify(analyzedTables)}`);
  const d1Table =
    (analyzed.tables || []).find((table) => String(table.file || '').startsWith('D1'))?.table ??
    analyzed.tables?.[0]?.table;
  if (!d1Table) throw new Error('分析结果缺少 D1 表');

  const firstMatch = await matchRespPromise;
  const firstMatchJson = await firstMatch.json();
  const expectedFirstLabel = TEMPLATE_STATUS_LABELS[firstMatchJson.status];
  check('首次匹配返回可解释状态', Boolean(expectedFirstLabel), firstMatchJson.status);
  log(
    `  [诊断] 首次匹配：status=${firstMatchJson.status} template=${firstMatchJson.template_name ?? '—'}（全局模板也会参与匹配）`,
  );

  const templateCard = page.locator('[data-testid="template-card"]');
  await templateCard.waitFor();
  const statusTag = page.locator('[data-testid="template-status"]');
  await statusTag.waitFor();
  await waitUntil(
    async () => (await statusTag.innerText()).trim() === expectedFirstLabel,
    30000,
    `模板状态标签显示 ${expectedFirstLabel}`,
  );
  check(
    '模板状态标签与匹配接口状态一致',
    (await statusTag.innerText()).trim() === expectedFirstLabel,
    `UI=${(await statusTag.innerText()).trim()} API=${firstMatchJson.status}`,
  );

  // 另存为新模板
  await templateCard.locator('[data-testid="save-template"]').click();
  const saveModal = page.locator('.ant-modal', { hasText: '另存为新模板' });
  await saveModal.waitFor();
  const templateName = `E2E映射模板-${Date.now()}`;
  await saveModal.locator('input').fill(templateName);
  const saveRespPromise = page.waitForResponse(
    (r) => r.url().endsWith('/import/templates') && r.request().method() === 'POST',
  );
  const savedMatchPromise = page.waitForResponse((r) =>
    r.url().includes('/import/templates/match'),
  );
  await saveModal.locator('.ant-modal-footer button.ant-btn-primary').click();
  const saveResp = await saveRespPromise;
  check('模板保存接口返回 200', saveResp.status() === 200, `HTTP ${saveResp.status()}`);
  const saved = await saveResp.json();
  check('模板保存返回模板 ID', Boolean(saved.id), saved.id);
  await savedMatchPromise;
  await waitUntil(
    async () => (await statusTag.innerText()).trim() === '完全匹配',
    30000,
    '保存后模板状态变为完全匹配',
  );
  check('模板卡展示新模板并标记完全匹配', (await statusTag.innerText()).trim() === '完全匹配');

  // 查看映射详情
  await templateCard.locator('[data-testid="template-details"]').click();
  const details = page.locator('.ant-modal', { hasText: '映射模板详情' });
  await details.waitFor();
  const detailsText = await details.innerText();
  check('详情展示表映射', detailsText.includes('表映射'));
  check('详情展示关系映射', detailsText.includes('关系映射'));
  check('详情展示模板依赖字段', detailsText.includes('模板依赖字段'));
  await page.screenshot({ path: path.join(SHOTS, '07-template-details.png') });
  await details.locator('.ant-modal-footer button').click();
  await details.waitFor({ state: 'hidden' });

  // 套用模板
  await templateCard.locator('[data-testid="apply-template"]').click();
  await page.locator('[data-testid="template-applied"]').waitFor();
  check('套用模板后显示已套用标记', true);

  // 回归：重新匹配必须撤销旧模板，避免旧 template_id 继续决定提交内容
  const rematchRespPromise = page.waitForResponse((r) =>
    r.url().includes('/import/templates/match'),
  );
  await templateCard.locator('button:has-text("重新匹配")').click();
  await rematchRespPromise;
  await page.locator('[data-testid="template-not-applied"]').waitFor({ timeout: 15000 });
  check(
    '重新匹配后撤销旧模板套用状态',
    (await page.locator('[data-testid="template-applied"]').count()) === 0,
  );
  await templateCard.locator('[data-testid="apply-template"]').click();
  await page.locator('[data-testid="template-applied"]').waitFor();

  // ── 定向复现：主键 Select 交互 → 模板套用状态撤销 ───────────────────
  const pkSelect = page.locator(`[data-testid="pk-select-${d1Table}"] .ant-select`);
  await pkSelect.waitFor();
  const beforeText = (await pkSelect.innerText()).trim();
  await pkSelect.click();
  const dropdown = page.locator('.ant-select-dropdown:visible').last();
  await dropdown.waitFor();
  await dropdown.locator('.ant-select-item-option').first().waitFor();
  const optionTexts = await dropdown.locator('.ant-select-item-option').allInnerTexts();
  log(`  [诊断] 主键下拉选项=${JSON.stringify(optionTexts)}；当前值=${beforeText}`);
  const currentField = fieldName(beforeText);
  const targetIndex = optionTexts.findIndex((text) => fieldName(text) !== currentField);
  if (targetIndex < 0) throw new Error('主键下拉没有可切换的其他字段，无法验证手动修改');
  await dropdown.locator('.ant-select-item-option').nth(targetIndex).click();
  await dropdown.waitFor({ state: 'hidden' });
  const afterText = (await pkSelect.innerText()).trim();
  log(`  [诊断] 交互后主键值=${afterText}`);
  check('主键 Select 交互后值已变化', afterText !== beforeText, `${beforeText} → ${afterText}`);

  await page.locator('[data-testid="template-not-applied"]').waitFor({ timeout: 15000 });
  check(
    '修改映射后撤销模板套用标记',
    (await page.locator('[data-testid="template-applied"]').count()) === 0,
  );

  // 拦截提交请求：验证手动修改后模板不会再决定提交数据（不落库）
  const commitPattern = '**/api/v1/import/relational/commit';
  let capturedPayload = null;
  const commitHandler = async (route) => {
    capturedPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: 'e2e-intercept',
        records: 4,
        entities: 4,
        relations: 3,
        kg_nodes: 4,
        kg_edges: 0,
        failed_rows: 0,
        errors: [],
        template: null,
      }),
    });
  };
  await page.route(commitPattern, commitHandler);
  await page.locator('[data-testid="commit-button"]').click();
  await waitUntil(() => capturedPayload !== null, 15000, '捕获手动修改后的提交载荷');
  check(
    '手动修改映射后提交不带 template_id',
    capturedPayload.template_id === undefined,
    String(capturedPayload.template_id),
  );
  check(
    '提交主键反映手动修改',
    capturedPayload.primary_keys?.[d1Table] === fieldName(afterText),
    `${capturedPayload.primary_keys?.[d1Table]} / ${fieldName(afterText)}`,
  );
  await page.unroute(commitPattern, commitHandler);

  // 重新套用模板，提交时应携带 template_id
  await templateCard.locator('[data-testid="apply-template"]').click();
  await page.locator('[data-testid="template-applied"]').waitFor();
  await page.screenshot({ path: path.join(SHOTS, '08-template-applied.png') });

  const commitReqPromise = page.waitForRequest(
    (r) => r.url().includes('/import/relational/commit'),
    { timeout: 120000 },
  );
  const commitRespPromise = page.waitForResponse(
    (r) => r.url().includes('/import/relational/commit'),
    { timeout: 300000 },
  );
  await page.locator('[data-testid="commit-button"]').click();
  const commitReq = await commitReqPromise;
  const commitPayload = commitReq.postDataJSON();
  check(
    '重新套用模板后导入请求携带 template_id',
    commitPayload.template_id === saved.id,
    String(commitPayload.template_id),
  );
  const commitResp = await commitRespPromise;
  const commitJson = await commitResp.json();
  // 真实 fixtures：D1 术语 5 行 + D2 中成药 3 行 = 8 个实体；D3 关联 4 行 = 4 条关系
  check(
    '模板辅助导入成功',
    commitResp.status() === 200 &&
      commitJson.entities === 8 &&
      commitJson.relations === 4 &&
      commitJson.failed_rows === 0,
    `HTTP ${commitResp.status()} entities=${commitJson.entities} relations=${commitJson.relations} failed=${commitJson.failed_rows}`,
  );
  await page.waitForSelector('text=导入完成', { timeout: 30000 });
  check('页面显示导入完成统计', true);
}

/** 索引一致性流程：环境 / 只读检查 / 安全门禁 / 拦截式操作 / 权限与跨环境拒绝。 */
async function indexFlow(page, kbName, kbId) {
  log('\n[2] 索引一致性：环境 / 只读检查 / 安全门禁 / 拦截式操作');
  const DIFF_RE = /\/api\/v1\/import\/index\/diff(\?|$)/;
  const APPLY_RE = /\/api\/v1\/import\/index\/apply(\?|$)/;
  const REBUILD_RE = /\/api\/v1\/import\/index\/rebuild(\?|$)/;
  const envUrl = `${BASE}/api/v1/import/index/environment`;
  const applyUrl = `${BASE}/api/v1/import/index/apply`;

  let applyRequests = 0;
  page.on('request', (request) => {
    if (APPLY_RE.test(request.url())) applyRequests += 1;
  });

  const envRespPromise = page.waitForResponse((r) =>
    r.url().includes('/import/index/environment'),
  );
  await page.goto(`${BASE}/admin/index-consistency`);
  const envResp = await envRespPromise;
  const envJson = await envResp.json();
  check(
    '环境接口返回隔离集合',
    envJson.milvus_collection === EXPECTED_COLLECTION,
    envJson.milvus_collection,
  );
  check('隔离环境允许删除向量（前端仍需二次确认）', envJson.destructive_allowed === true);
  check('环境接口指向开发数据库', envJson.database === 'kp_dev', envJson.database);
  await page.waitForSelector(`text=${EXPECTED_COLLECTION}`, { timeout: 30000 });
  const envBody = await page.locator('body').innerText();
  check(
    '页面显示当前集合与环境标识',
    envBody.includes('隔离集合') && envBody.includes('kp_dev') && envBody.includes('允许删除向量'),
  );
  check('页面显示索引一致性入口标题', envBody.includes('索引一致性检查'));

  const diffCard = page.locator('.ant-card', {
    has: page.locator('.ant-card-head-title', { hasText: '1. 只读差异检查' }),
  });
  await chooseOption(page, diffCard.locator('.ant-select'), kbName);

  const diffReqPromise = page.waitForRequest(DIFF_RE, { timeout: 60000 });
  const diffRespPromise = page.waitForResponse(DIFF_RE, { timeout: 120000 });
  await diffCard.locator('button:has-text("开始检查")').click();
  const diffReq = await diffReqPromise;
  const diffUrl = new URL(diffReq.url());
  check(
    '只读检查请求绑定当前集合',
    diffUrl.searchParams.get('expected_milvus_collection') === EXPECTED_COLLECTION,
    diffUrl.searchParams.toString(),
  );
  const diffResp = await diffRespPromise;
  check('差异检查返回 200', diffResp.status() === 200, `HTTP ${diffResp.status()}`);
  const diffJson = await diffResp.json();
  check('差异报告包含统计与文档明细', Boolean(diffJson.totals) && Array.isArray(diffJson.documents));
  await page.waitForSelector('text=扫描文档', { timeout: 30000 });
  check('页面展示扫描文档统计', true);
  await page.screenshot({ path: path.join(SHOTS, '09-index-diff.png') });

  const fixCard = page.locator('.ant-card', {
    has: page.locator('.ant-card-head-title', { hasText: '2. 修复与重建' }),
  });
  const orphanCheckbox = fixCard.locator('label', { hasText: '删除孤儿向量' }).locator('input');
  const duplicateCheckbox = fixCard.locator('label', { hasText: '删除重复向量' }).locator('input');
  check('删除孤儿向量默认未勾选', !(await orphanCheckbox.isChecked()));
  check('删除重复向量默认未勾选', !(await duplicateCheckbox.isChecked()));
  check('无孤儿向量时删除入口禁用', await orphanCheckbox.isDisabled());
  check('无重复向量时删除入口禁用', await duplicateCheckbox.isDisabled());
  const fixCardText = await fixCard.innerText();
  check(
    '界面提前展示影响范围且声明不删除向量',
    fixCardText.includes('影响范围预览') &&
      fixCardText.includes('受影响文档') &&
      fixCardText.includes('不删除向量'),
  );

  // 修复操作：先验证二次确认；取消不得发起请求
  const applyBaseline = applyRequests;
  await fixCard.locator('button:has-text("执行修复")').click();
  const confirmModal = page.locator('.ant-modal', { hasText: '确认执行索引修复' });
  await confirmModal.waitFor();
  const modalText = await confirmModal.innerText();
  check('修复前展示影响范围预览', modalText.includes('本次操作影响范围'));
  check(
    '确认框展示目标环境与集合',
    modalText.includes('目标环境：dev') && modalText.includes(`集合：${EXPECTED_COLLECTION}`),
  );
  await page.screenshot({ path: path.join(SHOTS, '10-index-fix-confirm.png') });
  await confirmModal.locator('.ant-modal-footer button').first().click();
  await confirmModal.waitFor({ state: 'hidden' });
  check('取消确认不会发起修复请求', applyRequests === applyBaseline, `requests=${applyRequests}`);

  // 修复操作：请求拦截，确保不触达真实索引
  let applyPayload = null;
  let applyIntercepted = 0;
  const applyHandler = async (route) => {
    applyIntercepted += 1;
    applyPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        flags_updated: 0,
        orphans_deleted: 0,
        duplicates_deleted: 0,
        rebuild_marked: 0,
        skipped_deletions: 0,
      }),
    });
  };
  await page.route(APPLY_RE, applyHandler);
  const applyRefresh = page.waitForResponse(DIFF_RE, { timeout: 30000 }).catch(() => null);
  await fixCard.locator('button:has-text("执行修复")').click();
  await confirmModal.waitFor();
  await confirmModal.locator('.ant-modal-footer button.ant-btn-primary').click();
  await waitUntil(() => applyPayload !== null, 15000, '捕获修复请求');
  check('修复请求已被测试拦截', applyIntercepted === 1, `intercepted=${applyIntercepted}`);
  check(
    '修复请求绑定当前知识库与集合',
    applyPayload.kb_id === kbId &&
      applyPayload.expected_milvus_collection === EXPECTED_COLLECTION,
  );
  check(
    '修复请求默认不删除向量',
    applyPayload.fix_flags === true &&
      applyPayload.delete_orphans === false &&
      applyPayload.delete_duplicates === false &&
      applyPayload.confirmed === false,
  );
  await applyRefresh;
  await page.waitForSelector('text=修复完成', { timeout: 30000 });
  check('拦截式修复返回后展示结果', true);
  await page.unroute(APPLY_RE, applyHandler);

  // 重建 / 重试：同样拦截，不执行真实索引重建
  let rebuildPayload = null;
  let rebuildIntercepted = 0;
  const rebuildHandler = async (route) => {
    rebuildIntercepted += 1;
    rebuildPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ rebuild_marked: 0, retry_failed: 0 }),
    });
  };
  await page.route(REBUILD_RE, rebuildHandler);
  const rebuildRefresh = page.waitForResponse(DIFF_RE, { timeout: 30000 }).catch(() => null);
  await fixCard.locator('button:has-text("标记缺失文档重建")').click();
  const rebuildModal = page.locator('.ant-modal', { hasText: '确认标记重建' });
  await rebuildModal.waitFor();
  check(
    '重建确认框说明不会删除向量',
    (await rebuildModal.innerText()).includes('不会删除任何向量'),
  );
  await rebuildModal.locator('.ant-modal-footer button.ant-btn-primary').click();
  await waitUntil(() => rebuildPayload !== null, 15000, '捕获重建请求');
  check('重建请求已被测试拦截', rebuildIntercepted === 1, `intercepted=${rebuildIntercepted}`);
  check(
    '重建请求绑定当前集合且不重试失败项',
    rebuildPayload.kb_id === kbId &&
      rebuildPayload.expected_milvus_collection === EXPECTED_COLLECTION &&
      rebuildPayload.retry_failed === false,
  );
  await rebuildRefresh;
  await page.waitForSelector('text=已标记重建', { timeout: 30000 });
  check('拦截式重建返回后展示结果', true);

  let retryPayload = null;
  let retryIntercepted = 0;
  const retryHandler = async (route) => {
    retryIntercepted += 1;
    retryPayload = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ rebuild_marked: 0, retry_failed: 0 }),
    });
  };
  await page.route(REBUILD_RE, retryHandler);
  const retryRefresh = page.waitForResponse(DIFF_RE, { timeout: 30000 }).catch(() => null);
  await fixCard.locator('button:has-text("重置失败文档重试")').click();
  const retryModal = page.locator('.ant-modal', { hasText: '确认重置失败文档' });
  await retryModal.waitFor();
  await retryModal.locator('.ant-modal-footer button.ant-btn-primary').click();
  await waitUntil(() => retryPayload !== null, 15000, '捕获重试请求');
  check('重试请求已被测试拦截', retryIntercepted === 1, `intercepted=${retryIntercepted}`);
  check(
    '重试请求使用 retry_failed=true 且绑定当前集合',
    retryPayload.retry_failed === true &&
      retryPayload.expected_milvus_collection === EXPECTED_COLLECTION,
  );
  await retryRefresh;
  await page.waitForSelector('text=已重置失败文档', { timeout: 30000 });
  check('拦截式重试返回后展示结果', true);
  await page.unroute(REBUILD_RE, retryHandler);

  // 危险删除 UI 门禁：用合成报告验证，不触发任何真实删除
  const syntheticReport = {
    kb_id: kbId,
    scanned_documents: 1,
    milvus_available: true,
    error: '',
    clean: false,
    totals: { missing_vectors: 1, orphan_vectors: 2, duplicate_vectors: 1, inaccurate_flags: 1 },
    documents: [
      {
        doc_id: 'e2e-doc-1',
        file_name: 'e2e-synthetic.pdf',
        expected_chunks: 1,
        actual_vectors: 2,
        missing_count: 1,
        orphan_count: 2,
        duplicate_count: 1,
        missing_chunk_ids: ['chunk-missing'],
        orphan_vector_ids: ['vec-orphan-1', 'vec-orphan-2'],
        duplicate_vector_ids: ['vec-dup-1'],
        current_status: 'indexed',
        suggested_status: 'stale',
        flag_inaccurate: true,
      },
    ],
  };
  const syntheticHandler = async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(syntheticReport),
    });
  };
  await page.route(DIFF_RE, syntheticHandler);
  const syntheticResp = page.waitForResponse(DIFF_RE, { timeout: 30000 });
  await diffCard.locator('button:has-text("开始检查")').click();
  await syntheticResp;
  await page.waitForSelector('text=删除孤儿向量（2 条）', { timeout: 30000 });
  await page.unroute(DIFF_RE, syntheticHandler);
  check('合成报告下删除入口可用', !(await orphanCheckbox.isDisabled()));
  check(
    '合成报告下删除仍默认关闭',
    !(await orphanCheckbox.isChecked()) && !(await duplicateCheckbox.isChecked()),
  );

  await fixCard.locator('label', { hasText: '删除孤儿向量' }).click();
  await waitUntil(
    async () => (await fixCard.innerText()).includes('删除 2 条孤儿向量'),
    10000,
    '影响范围反映删除选项',
  );
  check('影响范围预览随危险选项更新', (await fixCard.innerText()).includes('删除 2 条孤儿向量'));

  const ackBaseline = applyRequests;
  await fixCard.locator('button:has-text("执行修复")').click();
  const ackModal = page.locator('.ant-modal', { hasText: '确认执行索引修复' });
  await ackModal.waitFor();
  check(
    '危险操作要求显式风险确认',
    (await ackModal.innerText()).includes('我已了解删除的向量不可恢复'),
  );
  const ackOk = ackModal.locator('.ant-modal-footer button.ant-btn-primary');
  check('未勾选风险确认时执行按钮禁用', await ackOk.isDisabled());
  await page.screenshot({ path: path.join(SHOTS, '11-index-destructive-ack.png') });
  await ackModal.locator('label', { hasText: '我已了解删除的向量不可恢复' }).click();
  check('勾选风险确认后允许执行', !(await ackOk.isDisabled()));
  await ackModal.locator('.ant-modal-footer button').first().click();
  await ackModal.waitFor({ state: 'hidden' });
  check('危险操作取消后未发起请求', applyRequests === ackBaseline, `requests=${applyRequests}`);
  await fixCard.locator('label', { hasText: '删除孤儿向量' }).click();

  check(
    '修复/重建/重试请求均被拦截（未触达真实索引）',
    applyIntercepted === 1 && rebuildIntercepted === 1 && retryIntercepted === 1,
  );

  // 跨环境：声明正式集合必须被拒绝（后端仅做门禁校验，不执行操作）
  const staleApply = await page.request.post(applyUrl, {
    data: {
      kb_id: kbId,
      fix_flags: false,
      expected_milvus_collection: 'document_chunks',
    },
    headers: await authHeaders(page),
  });
  check('跨环境修复请求被 409 拒绝', staleApply.status() === 409, `HTTP ${staleApply.status()}`);
  const staleDiff = await page.request.get(
    `${BASE}/api/v1/import/index/diff?kb_id=${kbId}&expected_milvus_collection=document_chunks`,
    { headers: await authHeaders(page) },
  );
  check('跨环境只读检查同样被 409 拒绝', staleDiff.status() === 409, `HTTP ${staleDiff.status()}`);

  // 权限：未登录 401；member 403
  const anonEnv = await page.request.get(envUrl);
  check('未登录访问索引环境被 401', anonEnv.status() === 401, `HTTP ${anonEnv.status()}`);

  const stamp = Date.now();
  const memberName = `e2e-member-${stamp}`;
  const memberEmail = `${memberName}@biyesheji.dev`;
  const adminHeaders = await authHeaders(page);
  const createMember = await page.request.post(`${BASE}/api/v1/users`, {
    data: { username: memberName, email: memberEmail, name: 'E2E只读成员', role: 'member' },
    headers: adminHeaders,
  });
  check('创建只读成员成功', createMember.status() === 201, `HTTP ${createMember.status()}`);
  const createdMember = await createMember.json();
  const memberLogin = await page.request.post(`${BASE}/api/v1/auth/login`, {
    data: { username_or_email: memberEmail, password: createdMember.initial_password },
  });
  check('只读成员登录成功', memberLogin.ok(), `HTTP ${memberLogin.status()}`);
  const memberToken = (await memberLogin.json()).access_token;
  const memberHeaders = { Authorization: `Bearer ${memberToken}` };
  const memberEnv = await page.request.get(envUrl, { headers: memberHeaders });
  check('member 访问索引环境被 403', memberEnv.status() === 403, `HTTP ${memberEnv.status()}`);
  const memberApply = await page.request.post(applyUrl, {
    data: {
      kb_id: kbId,
      fix_flags: false,
      expected_milvus_collection: EXPECTED_COLLECTION,
    },
    headers: memberHeaders,
  });
  check('member 执行索引修复被 403', memberApply.status() === 403, `HTTP ${memberApply.status()}`);
  const disableMember = await page.request.post(
    `${BASE}/api/v1/users/${createdMember.user.id}/disable`,
    { headers: adminHeaders },
  );
  check('测试成员已禁用', disableMember.ok(), `HTTP ${disableMember.status()}`);
}

let browser;
let context;
let suiteTimer;
let shuttingDown = false;

async function shutdown(reason) {
  if (shuttingDown) return;
  shuttingDown = true;
  log(`关闭浏览器：${reason}`);
  if (context) await context.close().catch(() => {});
  if (browser) await browser.close().catch(() => {});
}

async function main() {
  suiteTimer = setTimeout(() => {
    process.exitCode = 1;
    log(`整套测试超时（${SUITE_TIMEOUT_MS}ms），强制关闭浏览器并结束`);
    void shutdown('整套超时');
  }, SUITE_TIMEOUT_MS);

  try {
    browser = await chromium.launch();
    context = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
    const page = await context.newPage();
    page.setDefaultTimeout(20000);
    page.on('pageerror', (err) => log(`  [PAGEERROR] ${err.message}`));
    page.on('console', (msg) => {
      if (msg.type() === 'error') log(`  [CONSOLE-ERROR] ${msg.text()}`);
    });
    page.on('requestfailed', (request) => {
      const url = request.url();
      if (url.includes('/api/v1/import/')) {
        log(`  [REQUEST-FAILED] ${url} — ${request.failure()?.errorText}`);
      }
    });

    log(`运行模式：${FOCUS}；日志：${LOG_PATH}`);

    await step('登录', async () => {
      await page.goto(`${BASE}/login`);
      await page.fill('input#username_or_email', EMAIL);
      await page.fill('input#password', PASSWORD);
      await page.click('button[type=submit]');
      await page.waitForURL('**/chat', { timeout: 60000 });
      check('管理员登录成功', page.url().includes('/chat'), page.url());
    }, 90000);

    const kbName = `E2E阶段三-${Date.now()}`;
    const kbId = await createKb(page, kbName);
    log(`测试知识库：${kbName} (${kbId})`);

    if (FOCUS === 'template' || FOCUS === 'all') {
      await step('映射模板流程', () => templateFlow(page, kbName), 5 * 60 * 1000);
    }
    if (FOCUS === 'index' || FOCUS === 'all') {
      await step('索引一致性流程', () => indexFlow(page, kbName, kbId), 3 * 60 * 1000);
    }
    if (!['template', 'index', 'all'].includes(FOCUS)) {
      throw new Error(`未知 focus 模式：${FOCUS}（可选 template / index / all）`);
    }
  } catch (error) {
    process.exitCode = 1;
    log(`测试异常终止：${error?.stack || error}`);
    check('测试脚本无异常', false, error?.message || String(error));
  } finally {
    if (suiteTimer) clearTimeout(suiteTimer);
    await shutdown('测试结束');
    const passed = results.filter((r) => r.ok).length;
    log(`\n结果：${passed}/${results.length} 通过`);
    log(`日志已保存：${LOG_PATH}`);
    if (passed !== results.length) process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
