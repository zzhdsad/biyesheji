/**
 * 阶段十六：node:test 的模块解析钩子（零新增依赖）。
 *
 * 目的：让 `node --test` 能直接加载 src 下的 .test.ts，并支持源码里
 * 使用的 `@/*` 路径别名（等价于 tsconfig paths: { "@/*": ["./src/*"] }）。
 *
 * 为什么不用 jest / vitest：
 * - 项目既有测试（src/utils/evidence.test.ts）已经跑在 node:test 上；
 * - 阶段十六要求“不要为了测试引入不必要依赖”，保持与 Node 24 原生 TS 支持一致。
 *
 * 用法（由 npm run test 内部使用）：
 *   node --test --import ./tests/alias-loader.mjs src/**\/*.test.ts
 */
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { register } from 'node:module';

const SRC_DIR = path.resolve(process.cwd(), 'src');

/** '@/' → 'src/'，并按 ESM 规则补齐扩展名（.ts / .tsx / index.ts）。 */
const ALIAS_EXTENSIONS = ['', '.ts', '.tsx', '/index.ts', '/index.tsx'];

function tryFile(targetPath) {
  for (const ext of ALIAS_EXTENSIONS) {
    const candidate = `${targetPath}${ext}`;
    if (existsSync(candidate)) return pathToFileURL(candidate).href;
  }
  return null;
}

/** @type {(url: string, context: import('node:module').ResolveHookContext, nextResolve: Function) => Promise<{ url: string, shortCircuit?: boolean, format?: string }>} */
export async function resolve(specifier, context, nextResolve) {
  // 注意：不要指定 format —— 让 Node 按扩展名识别 .ts 并做类型擦除
  // （强行 format:'module' 会导致 TS 语法被当作 JS 解析而报错）。
  if (specifier.startsWith('@/')) {
    const resolved = tryFile(path.join(SRC_DIR, specifier.slice(2)));
    if (resolved) return { url: resolved, shortCircuit: true };
  }
  // TS 源码里允许省略扩展名（import ... from './token'），ESM 默认不支持，
  // 这里按 TS 规则补齐（等同 tsconfig 的 moduleResolution: bundler）。
  if (specifier.startsWith('./') || specifier.startsWith('../')) {
    if (context.parentURL?.startsWith('file:')) {
      const parentPath = fileURLToPath(context.parentURL);
      const resolved = tryFile(path.resolve(path.dirname(parentPath), specifier));
      if (resolved) return { url: resolved, shortCircuit: true };
    }
  }
  return nextResolve(specifier, context);
}

register('./alias-loader.mjs', import.meta.url);
