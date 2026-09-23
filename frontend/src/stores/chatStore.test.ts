/**
 * 阶段十六：chatStore 状态机测试（流式问答链路）。
 *
 * 运行：npm test（node --test，零新增测试依赖）
 *
 * 目标不是重复 SSE 解析测试，而是验证**最终 ChatMessage 状态正确**：
 * - start / citations / delta / done / error 五类事件对消息属性的影响
 * - 阶段十五遗留问题：done.answer 必须覆盖 revise 前已流式渲染的文本
 * - evidence / evidence_groups / evidence_summary / kg_evidence /
 *   evidence_gate / reflection 均不丢失，且不被其它消息污染
 * - Gate disabled（evidence_gate=null）与 Reflection disabled（reflection=null）
 * - 错误与中断后的状态恢复（可再次发送）
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, describe, it } from 'node:test';

import type { ChatMessage } from '@/types';
import { useChatStore } from '@/stores/chatStore';

// ── 最小浏览器环境桩（localStorage / token 缓存依赖）──────────────────────

class FakeStorage {
  private data = new Map<string, string>();
  getItem(key: string) {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    this.data.set(key, String(value));
  }
  removeItem(key: string) {
    this.data.delete(key);
  }
  clear() {
    this.data.clear();
  }
}

const storage = new FakeStorage();

const globalAny = globalThis as unknown as Record<string, unknown>;

globalAny.localStorage = storage;
globalAny.window = { localStorage: storage, location: { href: 'http://localhost:3000/chat' } };
storage.setItem('kp_selected_kb_ids', JSON.stringify([]));

function sseStream(
  text: string,
  opts: { fail?: boolean } = {},
): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(text);
  return new ReadableStream<Uint8Array>({
    start(controller) {
      // fail=true：模拟读取过程中连接异常（network down），交由 store 的兜底处理
      if (opts.fail) {
        controller.error(new Error('network down'));
        return;
      }
      if (bytes.length > 0) controller.enqueue(bytes);
      controller.close();
    },
  });
}

let lastRequestBodies: unknown[] = [];

function mockFetch(
  text: string,
  opts: { status?: number; failBody?: boolean } = {},
): void {
  lastRequestBodies = [];
  globalAny.fetch = async (_url: string, init: { body?: string }) => {
    lastRequestBodies.push(init?.body ? JSON.parse(init.body) : null);
    const status = opts.status ?? 200;
    if (status >= 400) {
      return { ok: false, status, body: null };
    }
    return { ok: true, status, body: sseStream(text, { fail: opts.failBody }) };
  };
}

/** 构造一个完整 SSE 流文本。 */
function flow(events: [string, unknown][]): string {
  return events
    .map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`)
    .join('');
}

const CITATION_DOC = {
  chunk_id: 'c1',
  source_index: 1,
  doc_id: 'd1',
  doc_name: '伤寒论.pdf',
  page_num: 3,
  content: '太阳病，头痛发热',
  score: 0.9,
  source_kind: 'document',
  source_label: '文档',
};
const CITATION_KG = {
  chunk_id: 'kg:e1',
  source_index: 2,
  doc_id: 'kg:e1',
  doc_name: '金银花',
  content: '银翘散 → contains → 金银花',
  score: 0.88,
  source_kind: 'kg',
  source_label: '图谱·中药',
  resource_type: 'herb',
};

const EVIDENCE_GROUPS = [
  { group_key: 'document', source_kind: 'document', source_label: '文档' },
  { group_key: 'kg:herb', source_kind: 'kg', source_label: '知识图谱·中药' },
];
const EVIDENCE_SUMMARY = {
  evidence_count: 2,
  source_count: 2,
  group_count: 2,
  max_score: 0.9,
  by_level: { high: 2, medium: 0, insufficient: 0 },
};

function standardFlow(overrides: {
  deltaTexts?: string[];
  done?: Record<string, unknown>;
  cite?: Record<string, unknown>;
  includeStart?: boolean;
} = {}): string {
  const deltaTexts = overrides.deltaTexts ?? ['金银花清热解毒'];
  const events: [string, unknown][] = [];
  if (overrides.includeStart !== false) {
    events.push([
      'start',
      {
        conversation_id: 'conv-1',
        query_analysis: { question_type: 'herb', question_type_label: '中药', analyzer_version: 'analyzer-v3' },
        router_decision: { strategy_name: 'herb_focused', router_version: 'rule-v2' },
      },
    ]);
  }
  events.push([
    'citations',
    {
      citations: [CITATION_DOC, CITATION_KG],
      evidence: [CITATION_DOC, CITATION_KG],
      evidence_groups: EVIDENCE_GROUPS,
      evidence_summary: EVIDENCE_SUMMARY,
      kg_evidence: [CITATION_KG],
      evidence_gate: { decision: 'accept', gate_version: 'gate-v1', accepted_count: 2 },
      ...(overrides.cite ?? {}),
    },
  ]);
  for (const content of deltaTexts) events.push(['delta', { content }]);
  events.push([
    'done',
    {
      conversation_id: 'conv-1',
      message_id: 'msg-final',
      answer: deltaTexts.join(''),
      reflection: {
        decision: 'accept',
        reason: 'supported_by_evidence',
        reflection_version: 'reflection-v1',
        confidence: 0.9,
        issues: [],
        gate_retry_count: 0,
        reflection_retry_count: 0,
        total_retry_count: 0,
        revised: false,
        retried: false,
        is_valid: true,
        llm_used: false,
      },
      ...(overrides.done ?? {}),
    },
  ]);
  return flow(events);
}

/** store 单例（zustand 的 hook 即 v4 的 useBoundStore，可直接调用 API）。 */
const chatStore = useChatStore;

function resetStore(): void {
  chatStore.setState({
    knowledgeBases: [{ id: 'kb-1', name: '测试知识库' } as never],
    selectedKbIds: [],
    conversations: [],
    currentConversationId: null,
    messages: [],
    loadingKbs: false,
    loadingConversations: false,
    sending: false,
    health: null,
  });
}

function assistantMessages(): ChatMessage[] {
  return chatStore.getState().messages.filter((m) => m.role === 'assistant');
}

type StoreState = ReturnType<typeof chatStore.getState>;

function lastState(): StoreState {
  return chatStore.getState();
}

beforeEach(() => {
  resetStore();
  storage.clear();
});

afterEach(() => {
  resetStore();
});

describe('阶段十六：chatStore 正常问答链路', () => {
  it('完整流程：用户消息 + assistant 占位 → 流式拼接 → done 落地', async () => {
    mockFetch(standardFlow({ deltaTexts: ['金银花', '清热解毒', '。'] }));
    await chatStore.getState().sendMessage('金银花有什么功效？');

    const messages = lastState().messages;
    assert.equal(messages.length, 2);
    assert.equal(messages[0].role, 'user');
    assert.equal(messages[0].content, '金银花有什么功效？');

    const assistant = messages[1];
    assert.equal(assistant.role, 'assistant');
    assert.equal(assistant.content, '金银花清热解毒。');
    assert.equal(assistant.id, 'msg-final');
    assert.equal(lastState().sending, false);
    assert.equal(lastState().currentConversationId, 'conv-1');
  });

  it('start 事件写入 queryAnalysis 与 routerDecision', async () => {
    mockFetch(standardFlow());
    await chatStore.getState().sendMessage('问题');

    const assistant = assistantMessages()[0];
    assert.equal(assistant.queryAnalysis?.question_type, 'herb');
    assert.equal(assistant.queryAnalysis?.question_type_label, '中药');
    assert.equal(assistant.routerDecision?.strategy_name, 'herb_focused');
  });

  it('citations 事件写入 evidence / groups / summary / kgEvidence / evidenceGate', async () => {
    mockFetch(standardFlow());
    await chatStore.getState().sendMessage('问题');

    const assistant = assistantMessages()[0];
    assert.equal(assistant.citations?.length, 2);
    assert.equal(assistant.evidence?.length, 2);
    assert.equal(assistant.evidenceGroups?.length, 2);
    assert.deepEqual(assistant.evidenceSummary, EVIDENCE_SUMMARY);
    // 阶段十三：KG 证据切片不再被丢弃
    assert.equal(assistant.kgEvidence?.length, 1);
    assert.equal(assistant.kgEvidence?.[0].source_kind, 'kg');
    assert.equal(assistant.evidenceGate?.decision, 'accept');
    assert.equal(assistant.evidenceGate?.gate_version, 'gate-v1');
  });

  it('delta 按到达顺序拼接（打字机效果）', async () => {
    mockFetch(standardFlow({ deltaTexts: ['甲', '乙', '丙', '丁'] }));
    await chatStore.getState().sendMessage('问题');
    assert.equal(assistantMessages()[0].content, '甲乙丙丁');
  });

  it('请求体带上 kb_ids 与 conversation_id', async () => {
    mockFetch(standardFlow());
    await chatStore.getState().sendMessage('问题');
    const body = lastRequestBodies[0] as Record<string, unknown>;
    assert.deepEqual(body.kb_ids, ['kb-1']);
    assert.equal(body.question, '问题');
  });

  it('保存 by sendMessage 的 reflection 决策（accept）', async () => {
    mockFetch(standardFlow());
    await chatStore.getState().sendMessage('问题');
    const reflection = assistantMessages()[0].reflection;
    assert.ok(reflection);
    assert.equal(reflection.decision, 'accept');
    assert.equal(reflection.reflection_version, 'reflection-v1');
    assert.deepEqual(reflection.issues, []);
    assert.equal(reflection.total_retry_count, 0);
  });
});

describe('阶段十六：Self Reflection 三种决策的前端落地', () => {
  it('revise：done.answer 覆盖 revise 前已渲染的文本', async () => {
    const streamed = '金银花可以治疗失眠和高血压，效果确切。';
    const finalAnswer = '（已复核）资料仅记载金银花清热解毒。[citation: 1, 0]';
    mockFetch(
      standardFlow({
        deltaTexts: [streamed],
        done: {
          answer: finalAnswer,
          reflection: {
            decision: 'revise',
            reason: 'unsupported_sentences+no_citation_for_factual_answer',
            reflection_version: 'reflection-v1',
            revised: true,
            retried: false,
            gate_retry_count: 0,
            reflection_retry_count: 0,
            total_retry_count: 0,
            issues: ['unsupported_sentences', 'no_citation_for_factual_answer'],
            is_valid: true,
          },
        },
      }),
    );
    await chatStore.getState().sendMessage('金银花能治什么？');

    const assistant = assistantMessages()[0];
    assert.equal(assistant.content, finalAnswer, 'ChatMessage.content 必须等于最终权威答案');
    assert.notEqual(assistant.content, streamed);
    assert.equal(assistant.reflection?.decision, 'revise');
    assert.equal(assistant.reflection?.revised, true);
  });

  it('retry：reflection 计数落地，答案取最终答案', async () => {
    mockFetch(
      standardFlow({
        deltaTexts: ['初次答案'],
        done: {
          answer: '重试后的最终答案',
          reflection: {
            decision: 'retry',
            reason: 'cited_evidence_weak',
            reflection_version: 'reflection-v1',
            revised: false,
            retried: true,
            retry_strategy: 'baseline_hybrid',
            original_strategy: 'herb_focused',
            gate_retry_count: 1,
            reflection_retry_count: 1,
            total_retry_count: 2,
            gate_decision: 'accept',
            gate_version: 'gate-v1',
            issues: ['cited_evidence_weak'],
            is_valid: true,
          },
        },
      }),
    );
    await chatStore.getState().sendMessage('问题');

    const assistant = assistantMessages()[0];
    assert.equal(assistant.content, '重试后的最终答案');
    assert.equal(assistant.reflection?.decision, 'retry');
    assert.equal(assistant.reflection?.retried, true);
    assert.equal(assistant.reflection?.retry_strategy, 'baseline_hybrid');
    assert.equal(assistant.reflection?.original_strategy, 'herb_focused');
    assert.equal(assistant.reflection?.gate_retry_count, 1);
    assert.equal(assistant.reflection?.reflection_retry_count, 1);
    assert.equal(assistant.reflection?.total_retry_count, 2);
  });

  it('Reflection 失败（revised=false + is_valid=false）时保留原答案', async () => {
    const original = '黄芪降压效果显著。';
    mockFetch(
      standardFlow({
        deltaTexts: [original],
        done: {
          answer: original,
          reflection: {
            decision: 'accept',
            reason: 'reflection_unavailable',
            is_valid: false,
            fallback_reason: 'revision_failed',
            revised: false,
            reflection_version: 'reflection-v1',
            total_retry_count: 0,
          },
        },
      }),
    );
    await chatStore.getState().sendMessage('问题');

    const assistant = assistantMessages()[0];
    assert.equal(assistant.content, original);
    assert.equal(assistant.reflection?.is_valid, false);
    assert.equal(assistant.reflection?.fallback_reason, 'revision_failed');
  });

  it('Reflection disabled（reflection=null）不产生异常也不写入字段', async () => {
    mockFetch(standardFlow({ done: { reflection: null, answer: '旧版答案' } }));
    await chatStore.getState().sendMessage('问题');

    const assistant = assistantMessages()[0];
    assert.equal(assistant.content, '旧版答案');
    assert.equal(assistant.reflection, undefined, 'reflection 为 null 时不应写入属性');
  });

  it('Gate disabled（evidence_gate=null）：引用与答案正常，gate 属性不写入', async () => {
    mockFetch(
      standardFlow({
        cite: { evidence_gate: null },
        done: { reflection: null },
      }),
    );
    await chatStore.getState().sendMessage('问题');

    const assistant = assistantMessages()[0];
    assert.equal(assistant.citations?.length, 2);
    assert.equal(assistant.evidenceGroups?.length, 2);
    assert.equal(assistant.evidenceGate, undefined);
    assert.equal(assistant.reflection, undefined);
    assert.equal(lastState().sending, false);
  });

  it('Gate insufficient：拒答文案与 Gate 数据同时落地', async () => {
    const refusal = '根据当前知识库内容，我无法可靠回答该问题。';
    mockFetch(
      standardFlow({
        deltaTexts: [refusal],
        cite: {
          evidence_gate: {
            decision: 'insufficient',
            reason: 'no_evidence',
            gate_version: 'gate-v1',
            evidence_count: 0,
            accepted_count: 0,
            is_valid: true,
          },
        },
        done: { answer: refusal, reflection: null },
      }),
    );
    await chatStore.getState().sendMessage('无法回答的问题');

    const assistant = assistantMessages()[0];
    assert.equal(assistant.content, refusal);
    assert.equal(assistant.evidenceGate?.decision, 'insufficient');
    assert.equal(assistant.reflection, undefined);
  });
});

describe('阶段十六：错误、中断与状态恢复', () => {
  it('SSE error 事件：写入失败提示并结束 sending', async () => {
    mockFetch(
      flow([
        ['start', { conversation_id: 'c1' }],
        ['citations', { citations: [] }],
        ['error', { message: '回答生成失败：模型不可用' }],
      ]),
    );
    await chatStore.getState().sendMessage('问题');

    const assistant = assistantMessages()[0];
    assert.match(assistant.content, /回答生成失败：模型不可用/);
    assert.equal(lastState().sending, false);
  });

  it('error 事件到达时已有 delta：保留已生成内容，不覆盖', async () => {
    mockFetch(
      flow([
        ['start', { conversation_id: 'c1' }],
        ['delta', { content: '部分答案' }],
        ['error', { message: '流中断' }],
      ]),
    );
    await chatStore.getState().sendMessage('问题');
    assert.equal(assistantMessages()[0].content, '部分答案');
    assert.equal(lastState().sending, false);
  });

  it('HTTP 500：进入网络兜底，sending 恢复', async () => {
    mockFetch('', { status: 500 });
    await chatStore.getState().sendMessage('问题');
    assert.equal(assistantMessages()[0].content, '请求失败，请稍后重试。');
    assert.equal(lastState().sending, false);
  });

  it('读取流时网络异常：进入网络兜底，sending 恢复', async () => {
    mockFetch('anything', { failBody: true });
    await chatStore.getState().sendMessage('问题');
    assert.equal(assistantMessages()[0].content, '请求失败，请稍后重试。');
    assert.equal(lastState().sending, false);
  });

  it('阶段十六修复：流异常终止（无 done/error）也必须恢复可发送', async () => {
    // 流只到 delta 就结束（模拟连接被中断且未收到终止事件）
    mockFetch(flow([['start', { conversation_id: 'c1' }], ['delta', { content: '半截答案' }]]));
    await chatStore.getState().sendMessage('问题');
    assert.equal(lastState().sending, false, '中断后必须能再次发送');
    assert.equal(assistantMessages()[0].content, '半截答案');

    // 恢复后仍可继续提问
    mockFetch(standardFlow({ deltaTexts: ['第二次回答'] }));
    await chatStore.getState().sendMessage('再问一次');
    const messages = lastState().messages;
    assert.equal(messages.length, 4);
    assert.equal(messages[1].content, '半截答案');
    assert.equal(messages[3].content, '第二次回答');
  });

  it('无知识库时拒绝发送，不产生占位消息', async () => {
    chatStore.setState({ knowledgeBases: [], selectedKbIds: [] });
    mockFetch(standardFlow());
    await chatStore.getState().sendMessage('问题');
    assert.equal(lastState().messages.length, 0);
  });

  it('sending 期间重复发送被忽略', async () => {
    mockFetch(standardFlow());
    const first = chatStore.getState().sendMessage('第一次');
    await chatStore.getState().sendMessage('第二次');
    await first;
    const questions = lastState()
      .messages.filter((m) => m.role === 'user')
      .map((m) => m.content);
    assert.deepEqual(questions, ['第一次']);
  });
});

describe('阶段十六：多轮会话不互相污染', () => {
  it('第二轮数据只写入第二条 assistant 消息', async () => {
    mockFetch(standardFlow({ deltaTexts: ['第一轮答案'] }));
    await chatStore.getState().sendMessage('第一轮');
    const firstId = assistantMessages()[0].id;

    mockFetch(
      standardFlow({
        deltaTexts: ['第二轮答案'],
        done: {
          answer: '第二轮最终答案',
          reflection: {
            decision: 'revise',
            revised: true,
            reflection_version: 'reflection-v1',
            total_retry_count: 0,
          },
        },
      }),
    );
    await chatStore.getState().sendMessage('第二轮');

    const assistants = assistantMessages();
    assert.equal(assistants.length, 2);
    // 第一条消息保持第一轮状态，未被第二轮 revise 覆盖
    assert.equal(assistants[0].id, firstId);
    assert.equal(assistants[0].content, '第一轮答案');
    assert.equal(assistants[0].reflection?.decision, 'accept');
    // 第二条消息取第二轮最终答案
    assert.equal(assistants[1].content, '第二轮最终答案');
    assert.equal(assistants[1].reflection?.decision, 'revise');
  });

  it('切换会话清空消息，newConversation 不影响历史。', async () => {
    mockFetch(standardFlow({ deltaTexts: ['答案A'] }));
    await chatStore.getState().sendMessage('A');
    assert.equal(lastState().messages.length, 2);

    chatStore.getState().newConversation();
    assert.deepEqual(lastState().messages, []);
    assert.equal(lastState().currentConversationId, null);
  });

  it('selectedKbIds 变化时请求体随之变化（不会串用旧选择）', async () => {
    mockFetch(standardFlow());
    chatStore.getState().setSelectedKbIds(['kb-1']);
    await chatStore.getState().sendMessage('问题1');
    assert.deepEqual((lastRequestBodies[0] as Record<string, unknown>).kb_ids, ['kb-1']);

    chatStore.setState({ knowledgeBases: [{ id: 'kb-1' }, { id: 'kb-2' }] as never[] });
    chatStore.getState().setSelectedKbIds(['kb-2']);
    mockFetch(standardFlow());
    await chatStore.getState().sendMessage('问题2');
    assert.deepEqual((lastRequestBodies[0] as Record<string, unknown>).kb_ids, ['kb-2']);
  });
});
