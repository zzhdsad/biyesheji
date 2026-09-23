/**
 * 阶段十六：前后端 API 契约守卫测试。
 *
 * 运行：npm test（node --test，零新增测试依赖）
 *
 * 目的：防止「前端调用了后端不存在的端点」这类破坏在生产才被发现。做法是静态核对：
 * 1. 从 frontend/src/services/api.ts、auth.ts 中提取所有 axios 调用的方法 + 路径；
 * 2. 归一化路径中的模板变量（`${kbId}` → `{p}`）；
 * 3. 在 backend/src/api/routes/*.py 中查找匹配的同方法路由装饰器。
 *
 * 本测试只读源码、不发请求，因此不需要后端 / Milvus / Redis 可用。
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { describe, it } from 'node:test';

const FRONTEND_ROOT = process.cwd();
const REPO_ROOT = path.resolve(FRONTEND_ROOT, '..');
const BACKEND_ROUTES_DIR = path.resolve(REPO_ROOT, 'backend/src/api/routes');

/** 前端路径前缀 → 后端路由文件（用于精确定位，避免同名路径互相满足）。 */
const PREFIX_TO_ROUTE_FILE: Record<string, string> = {
  auth: 'auth.py',
  chat: 'chat.py',
  evaluation: 'evaluation.py',
  kb: 'knowledge_bases.py',
  'knowledge-bases': 'knowledge_bases.py',
  documents: 'documents.py',
  herbs: 'herbs.py',
  prescriptions: 'prescriptions.py',
  theories: 'theories.py',
  literatures: 'literatures.py',
  users: 'users.py',
  admin: 'admin.py',
  audit: 'audit.py',
  settings: 'settings.py',
  feedbacks: 'feedbacks.py',
  taxonomy: 'taxonomy.py',
  categories: 'taxonomy.py',
  tags: 'taxonomy.py',
  kg: 'kg.py',
};

/**
 * 已知缺失端点（阶段十六确认的真实缺陷，记录在此以便集中跟踪）。
 *
 * 这些端点前端已调用、但后端**尚未提供**。此处显式登记而非删除用例，
 * 保证：一旦后端补齐，本守卫会立刻失败并提示移除登记项。
 */
const KNOWN_MISSING: { method: string; path: string; note: string }[] = [
  {
    method: 'POST',
    path: '/auth/register',
    note: '阶段十六发现：注册页（/register）无后端端点，仅管理员可通过 POST /users 建账号',
  },
];

interface ApiCall {
  method: 'get' | 'post' | 'put' | 'patch' | 'delete';
  pathKey: string; // 模板变量已归一化为 {p}
  raw: string;
  source: string;
}

const CALL_RE =
  /(?:api|instance|request)\.(get|post|put|patch|delete)(?:<[^>]*>)?\(\s*[`'"]([^`'"]+)[`'"]/g;

/** `${kbId}` → `{p}`，路径标准化；同时去掉 `/api/v1` 前缀。 */
function normalizePath(raw: string): string {
  return raw
    .replace(/\$\{[^}]+\}/g, '{p}')
    .replace(/^\/api\/v1/, '')
    .replace(/\/$/, '');
}

function readFilesRecursively(dir: string, exts: string[]): { file: string; content: string }[] {
  const out: { file: string; content: string }[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...readFilesRecursively(full, exts));
    } else if (exts.some((ext) => entry.name.endsWith(ext))) {
      out.push({ file: full, content: fs.readFileSync(full, 'utf8') });
    }
  }
  return out;
}

function extractFrontendCalls(): ApiCall[] {
  const targets = [
    path.join(FRONTEND_ROOT, 'src/services/api.ts'),
    path.join(FRONTEND_ROOT, 'src/services/auth.ts'),
  ];
  const calls: ApiCall[] = [];
  for (const file of targets) {
    const content = fs.readFileSync(file, 'utf8');
    CALL_RE.lastIndex = 0;
    let match = CALL_RE.exec(content);
    while (match !== null) {
      const method = match[1] as ApiCall['method'];
      const raw = match[2];
      if (raw.startsWith('/')) {
        calls.push({ method, pathKey: normalizePath(raw), raw, source: path.basename(file) });
      }
      match = CALL_RE.exec(content);
    }
  }
  // SSE 专用端点（useSSE.ts 由 chatStore 调用，不是 axios 调用，单独登记）
  calls.push({
    method: 'post',
    pathKey: '/chat/ask-stream',
    raw: '/api/v1/chat/ask-stream',
    source: 'useSSE.ts(chatStore)',
  });
  return calls;
}

const ROUTE_FILES = readFilesRecursively(BACKEND_ROUTES_DIR, ['.py']);

/** 该文件里 APIRouter 声明的前缀（没有则为 null）。 */
function routerPrefix(content: string): string | null {
  const match = content.match(/APIRouter\(([^)]*)\)/);
  if (!match) return null;
  const prefix = match[1].match(/prefix\s*=\s*["']([^"']+)["']/);
  return prefix ? prefix[1] : null;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** 由归一化路径生成匹配后端装饰器的正则（`{p}` → `{任意参数名}`）。 */
function decoratorRegex(method: ApiCall['method'], pathKey: string): RegExp {
  const segments = pathKey.split('{p}').map(escapeRegExp);
  const pathPattern = segments.join('\\{[a-zA-Z_][a-zA-Z0-9_]*\\}');
  return new RegExp(`@router\\.${method}\\(\\s*["']${pathPattern}["']`);
}

function findBackendRoute(call: ApiCall): { file: string; matched: boolean } {
  const prefix = call.pathKey.split('/')[1] ?? '';
  const target = PREFIX_TO_ROUTE_FILE[prefix];
  const candidates = target
    ? ROUTE_FILES.filter((f) => f.file.endsWith(target))
    : ROUTE_FILES;

  for (const candidate of candidates) {
    // 各路由文件自带前缀（APIRouter(prefix="/chat")），装饰器路径是相对前缀的，
    // 因此先把前端路径去掉前缀再匹配。
    const prefix0 = routerPrefix(candidate.content);
    const relative =
      prefix0 && call.pathKey.startsWith(`${prefix0}/`)
        ? call.pathKey.slice(prefix0.length)
        : prefix0 && call.pathKey === prefix0
          ? ''
          : call.pathKey;
    if (relative === '' && !prefix0) continue;
    if (decoratorRegex(call.method, relative).test(candidate.content)) {
      return { file: path.basename(candidate.file), matched: true };
    }
  }
  return { file: target ?? '(all)', matched: false };
}

describe('阶段十六：前后端 API 契约守卫', () => {
  const calls = extractFrontendCalls();

  it('能从前端源码中抽取到足量的 API 调用（守卫有效性自检）', () => {
    // 阶段十六核对时前端共 70+ 个调用；低于该数量说明抽取正则失效，守卫形同虚设
    assert.ok(calls.length >= 50, `抽取到的调用过少：${calls.length}`);
    const paths = calls.map((c) => `${c.method.toUpperCase()} ${c.pathKey}`);
    for (const required of [
      'POST /chat/ask',
      'POST /chat/ask-stream',
      'GET /chat/conversations',
      'GET /chat/conversations/{p}/messages',
      'POST /evaluation/run',
      'GET /evaluation/results',
      'GET /kb',
      'GET /kb/{p}/resources',
      'GET /herbs',
      'GET /prescriptions',
      'GET /theories',
      'GET /literatures',
      'GET /categories',
      'GET /tags',
    ]) {
      assert.ok(paths.includes(required), `未抽取到调用：${required}`);
    }
  });

  it('除已登记缺陷外，前端调用的端点后端均已实现', () => {
    const missing = calls
      .map((call) => ({ call, result: findBackendRoute(call) }))
      .filter(({ call, result }) => {
        if (result.matched) return false;
        return !KNOWN_MISSING.some(
          (m) => m.method.toUpperCase() === call.method.toUpperCase() && m.path === call.pathKey,
        );
      });

    assert.deepEqual(
      missing.map(({ call, result }) => `${call.method.toUpperCase()} ${call.pathKey} (${call.source} → ${result.file})`),
      [],
      `前端调用了后端不存在的端点：${missing.length} 个`,
    );
  });

  it('已登记缺失端点确实仍然缺失（便于后端补齐后立刻发现）', () => {
    for (const item of KNOWN_MISSING) {
      const result = findBackendRoute({
        method: item.method.toLowerCase() as ApiCall['method'],
        pathKey: item.path,
        raw: item.path,
        source: 'known-missing',
      });
      assert.equal(result.matched, false, `${item.method} ${item.path} 已在后端实现，请从 KNOWN_MISSING 移除：${item.note}`);
    }
  });

  it('阶段十一至十五的 Chat 响应字段在前端类型中已声明', () => {
    const types = fs.readFileSync(path.join(FRONTEND_ROOT, 'src/types/index.ts'), 'utf8');
    for (const field of [
      'queryAnalysis',
      'routerDecision',
      'evidenceGate',
      'reflection',
      'evidenceGroups',
      'evidenceSummary',
      'kgEvidence',
      'reflection_retry_count',
      'total_retry_count',
    ]) {
      assert.ok(types.includes(field), `types/index.ts 缺少字段声明：${field}`);
    }
    const chatStoreSrc = fs.readFileSync(path.join(FRONTEND_ROOT, 'src/stores/chatStore.ts'), 'utf8');
    for (const token of ['kgEvidence', 'evidenceGate', 'reflection', 'answer']) {
      assert.ok(chatStoreSrc.includes(token), `chatStore.ts 未处理 ${token}`);
    }
  });

  it('SSE 事件名与后端保持一致', () => {
    const ragService = fs.readFileSync(
      path.resolve(REPO_ROOT, 'backend/src/application/rag_service.py'),
      'utf8',
    );
    const useSSE = fs.readFileSync(path.join(FRONTEND_ROOT, 'src/hooks/useSSE.ts'), 'utf8');
    for (const eventName of ['start', 'citations', 'delta', 'done', 'error']) {
      const backendYield = new RegExp(`"event"\\s*:\\s*"${eventName}"`);
      assert.ok(backendYield.test(ragService), `后端未产出 ${eventName} 事件`);
      assert.ok(
        new RegExp(`case '${eventName}':`).test(useSSE),
        `前端未处理 ${eventName} 事件`,
      );
    }
  });
});
