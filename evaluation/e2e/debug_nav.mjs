import { chromium } from 'playwright';

const browser = await chromium.launch();
const page = await browser.newPage();
page.on('framenavigated', (f) => {
  if (f === page.mainFrame()) console.log('NAV ->', f.url());
});

await page.goto('http://localhost:3000/login');
await page.fill('input#username_or_email', 'e2e@biyesheji.dev');
await page.fill('input#password', 'E2ePass123!');
await page.click('button[type=submit]');

for (let i = 0; i < 10; i++) {
  await page.waitForTimeout(1000);
  console.log(`t=${i + 1}s url=${page.url()}`);
}
console.log('BODY:', (await page.locator('body').innerText()).slice(0, 300).replace(/\n/g, ' | '));
await browser.close();
