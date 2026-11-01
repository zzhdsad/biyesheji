/**
 * 长测试外部监督器：在 phase3.mjs 内部计时之外再提供一层硬墙钟超时。
 *
 * 用法（cwd = evaluation/e2e）：
 *   node run-supervised.mjs 240000 --focus=index
 *
 * 超时后使用 taskkill /T /F 终止整个子进程树（含 Playwright Chromium），
 * 避免只杀 Node 留下浏览器子进程。退出码 124 表示外部超时。
 */
import { spawn, spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const limitMs = Number(args[0] && /^\d+$/.test(args[0]) ? args.shift() : 300000);
const child = spawn(process.execPath, ['phase3.mjs', ...args], {
  cwd: __dirname,
  stdio: 'inherit',
  env: process.env,
});

let killed = false;
function killTree() {
  if (killed || child.exitCode !== null) return;
  killed = true;
  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'inherit' });
  } else {
    child.kill('SIGKILL');
  }
}

const timer = setTimeout(() => {
  console.error(`[supervisor] 外部超时 ${limitMs}ms，终止测试进程树`);
  process.exitCode = 124;
  killTree();
}, limitMs);

child.on('exit', (code, signal) => {
  clearTimeout(timer);
  if (!killed) process.exitCode = code ?? (signal ? 1 : 0);
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    console.error(`[supervisor] 收到 ${signal}，终止测试进程树`);
    process.exitCode = 1;
    killTree();
  });
}
