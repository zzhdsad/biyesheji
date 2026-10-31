import { chromium } from 'playwright';

const browser = await chromium.launch();
const page = await browser.newPage();
page.on('console', (m) => console.log('CONSOLE:', m.text().slice(0, 200)));
page.on('pageerror', (e) => console.log('PAGEERROR:', e.message.slice(0, 200)));
page.on('response', async (r) => {
  if (r.url().includes('/api/')) {
    console.log('API', r.status(), r.url(), (await r.text().catch(() => '')).slice(0, 200));
  }
});
page.on('request', (r) => {
  if (r.url().includes('/api/')) console.log('REQ', r.method(), r.url());
});

await page.goto('http://localhost:3000/login');
console.log('inputs:', await page.locator('input').count());
console.log('ids:', await page.locator('input').evaluateAll((els) => els.map((e) => e.id + '|' + e.type + '|' + e.name)));

await page.fill('input#username_or_email', 'e2e@biyesheji.dev');
await page.fill('input#password', 'E2ePass123!');
await page.click('button[type=submit]');
await page.waitForTimeout(6000);
console.log('URL:', page.url());
const alert = await page.locator('.ant-alert').count();
if (alert) console.log('ALERT:', await page.locator('.ant-alert').first().innerText());
console.log('BODY:', (await page.locator('body').innerText()).slice(0, 500));
await browser.close();
