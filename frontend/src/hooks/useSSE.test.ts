/**
 * 阶段十六：SSE 流解析测试（Chat 链路）。
 *
 * 运行：npm test（node --test，零新增测试依赖）
 *
 * 覆盖后端 /chat/ask-stream 的真实事件协议：
 *   start → citations → delta × N → done（异常时 event: error）
 * 以及阶段十五遗留问题：done.answer 是最终权威答案。
 *
 * 说明：后端以纯 LF 行结束（backend/src/api/routes/chat.py:
 * `yield f"event: {evt['event']}\ndata: {data}\n\n"`），本测试按该真实格式构造流；
 * 同时覆盖了 chunk 被任意切分、未知字段、未知事件、坏 JSON 等兼容场景。
 */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { streamSSE } from './useSSE.ts';

/** 构造后端格式的事件片段（LF 行结束，事件以空行分隔）。 */
function event(name: string, data: unknown): string {
  return `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** 把 SSE 文本按指定大小切区块，模拟网络分片。 */
function toStream(text: string, chunkSize = 1): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  const bytes = encoder.encode(text);
  const chunks: Uint8Array[] = [];
  for (let i = 0; i < bytes.length; i += chunkSize) {
    chunks.push(bytes.subarray(i, i + chunkSize));
  }
  let index = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(chunks[index]);
      index += 1;
    },
  });
}

/** 安装一个假的全局 fetch，返回指定 SSE 文本（或 HTTP 错误）。 */
function mockSse(text: string, status = 200): void {
  (globalThis as { fetch?: unknown }).fetch = async () => ({
    ok: status >= 200 && status < 300,
    status,
    body: status >= 200 && status < 300 ? toStream(text, 3) : null,
  });
}

interface RecordedEvent {
  name: string;
  data: Record<string, unknown>;
}

async function collect(text: string, opts: { status?: number } = {}): Promise<{
  events: RecordedEvent[];
  error?: Error;
}> {
  mockSse(text, opts.status ?? 200);
  const events: RecordedEvent[] = [];
  const handlers = {
    onStart: (data: Record<string, unknown>) => events.push({ name: 'start', data }),
    onCitations: (data: Record<string, unknown>) => events.push({ name: 'citations', data }),
    onDelta: (data: Record<string, unknown>) => events.push({ name: 'delta', data }),
    onDone: (data: Record<string, unknown>) => events.push({ name: 'done', data }),
    onError: (data: Record<string, unknown>) => events.push({ name: 'error', data }),
  };
  try {
    await streamSSE('/api/v1/chat/ask-stream', { question: 'q' }, handlers);
  } catch (error) {
    return { events, error: error as Error };
  }
  return { events };
}

const CITATIONS = [
  {
    chunk_id: 'c1',
    source_index: 1,
    doc_id: 'd1',
    doc_name: '伤寒论.pdf',
    content: '太阳病',
    score: 0.9,
    source_kind: 'document',
    source_label: '文档',
  },
];

describe('阶段十六：SSE 事件顺序与解析', () => {
  it('正常回答：start → citations → delta×N → done', async () => {
    const text =
      event('start', { conversation_id: 'conv-1' }) +
      event('citations', { citations: CITATIONS, evidence_summary: { evidence_count: 1 } }) +
      event('delta', { content: '金银花' }) +
      event('delta', { content: '清热解毒' }) +
      event('done', { conversation_id: 'conv-1', message_id: 'msg-1', answer: '金银花清热解毒' });
    const { events } = await collect(text);

    assert.deepEqual(
      events.map((e) => e.name),
      ['start', 'citations', 'delta', 'delta', 'done'],
    );
    assert.equal(events[0].data.conversation_id, 'conv-1');
    assert.equal((events[1].data.citations as unknown[]).length, 1);
    assert.equal(events[2].data.content, '金银花');
    assert.equal(events[3].data.content, '清热解毒');
    assert.equal(events[4].data.answer, '金银花清热解毒');
  });

  it('阶段十五：done.answer 覆盖 revise 前的流式文本（最终权威答案）', async () => {
    const text =
      event('start', { conversation_id: 'c1' }) +
      event('citations', { citations: CITATIONS }) +
      event('delta', { content: '金银花主治顽固膝膝失眠' }) +
      event('done', {
        conversation_id: 'c1',
        message_id: 'm1',
        answer: '（已复核）仅记载金银花清热解毒。[citation: 1, 0]',
        reflection: { decision: 'revise', revised: true, reflection_version: 'reflection-v1' },
      });
    const { events } = await collect(text);

    const done = events.find((e) => e.name === 'done');
    assert.ok(done);
    assert.notEqual(done.data.answer, '金银花主治顽固膝膝失眠');
    assert.ok(String(done.data.answer).startsWith('（已复核）'));
    const reflection = done.data.reflection as Record<string, unknown>;
    assert.equal(reflection.decision, 'revise');
    assert.equal(reflection.revised, true);
  });

  it('citations 事件保留 KG 证据与 Evidence Gate 数据', async () => {
    const kg = [
      {
        chunk_id: 'kg:e1',
        source_index: 2,
        doc_id: 'kg:e1',
        doc_name: '金银花',
        content: '银翘散 → contains → 金银花',
        score: 0.9,
        source_kind: 'kg',
        source_label: '图谱·中药',
        resource_type: 'herb',
      },
    ];
    const text =
      event('citations', {
        citations: [...CITATIONS, ...kg],
        evidence: [...CITATIONS, ...kg],
        evidence_groups: [{ group_key: 'kg:herb' }, { group_key: 'document' }],
        evidence_summary: { evidence_count: 2, group_count: 2 },
        kg_evidence: kg,
        evidence_gate: { decision: 'accept', gate_version: 'gate-v1' },
      }) + event('done', { conversation_id: 'c', message_id: 'm' });
    const { events } = await collect(text);

    const citationsEvent = events.find((e) => e.name === 'citations');
    assert.ok(citationsEvent);
    assert.equal((citationsEvent.data.kg_evidence as unknown[]).length, 1);
    assert.equal((citationsEvent.data.citations as unknown[]).length, 2);
    assert.equal((citationsEvent.data.evidence_groups as unknown[]).length, 2);
    assert.deepEqual(citationsEvent.data.evidence_gate, {
      decision: 'accept',
      gate_version: 'gate-v1',
    });
  });

  it('Evidence Gate 关闭（evidence_gate=null）时不丢失其他字段', async () => {
    const text =
      event('citations', { citations: CITATIONS, evidence_gate: null }) +
      event('done', { conversation_id: 'c', message_id: 'm', reflection: null });
    const { events } = await collect(text);

    const citationsEvent = events.find((e) => e.name === 'citations');
    assert.equal(citationsEvent?.data.evidence_gate, null);
    const doneEvent = events.find((e) => e.name === 'done');
    assert.equal(doneEvent?.data.reflection, null);
  });

  it('Reflection 关闭（done 不带 answer/reflection）时仍正常结束', async () => {
    const text =
      event('start', { conversation_id: 'c' }) +
      event('citations', { citations: CITATIONS }) +
      event('delta', { content: '答案文本' }) +
      event('done', { conversation_id: 'c', message_id: 'm' });
    const { events } = await collect(text);

    const doneEvent = events.find((e) => e.name === 'done');
    assert.ok(doneEvent);
    assert.equal(doneEvent.data.answer, undefined);
    assert.equal(doneEvent.data.reflection, undefined);
  });

  it('未知字段与未知事件不影响既有解析', async () => {
    const text =
      event('start', { conversation_id: 'c', future_field: { nested: true } }) +
      event('token_usage', { total: 10 }) +
      event('citations', { citations: CITATIONS, unknown_prop: 1 }) +
      event('done', { conversation_id: 'c', message_id: 'm', extra: null });
    const { events } = await collect(text);

    assert.deepEqual(
      events.map((e) => e.name),
      ['start', 'citations', 'done'],
    );
    assert.deepEqual(events[0].data.future_field, { nested: true });
  });

  it('流式分块跨越事件边界时仍能正确拼接', async () => {
    const text =
      event('start', { conversation_id: 'conv-chunk' }) +
      event('delta', { content: '这是一段比较长的中文回答内容用于测试分块' }) +
      event('done', { conversation_id: 'conv-chunk', message_id: 'm', answer: '最终' });
    for (const size of [1, 2, 5, 17, 64, 1024]) {
      mockSse(text);
      const events: RecordedEvent[] = [];
      const bytes = new TextEncoder().encode(text);
      const chunks: Uint8Array[] = [];
      for (let i = 0; i < bytes.length; i += size) {
        chunks.push(bytes.subarray(i, i + size));
      }
      let index = 0;
      (globalThis as { fetch?: unknown }).fetch = async () => ({
        ok: true,
        status: 200,
        body: new ReadableStream<Uint8Array>({
          pull(controller) {
            if (index >= chunks.length) return controller.close();
            controller.enqueue(chunks[index]);
            index += 1;
          },
        }),
      });
      await streamSSE('/x', {}, {
        onStart: (d) => events.push({ name: 'start', data: d }),
        onDelta: (d) => events.push({ name: 'delta', data: d }),
        onDone: (d) => events.push({ name: 'done', data: d }),
      });
      assert.deepEqual(
        events.map((e) => e.name),
        ['start', 'delta', 'done'],
        `chunkSize=${size} 时事件顺序应稳定`,
      );
      assert.equal(events[1].data.content, '这是一段比较长的中文回答内容用于测试分块');
    }
  });

  it('多行 data 按 SSE 规范拼接', async () => {
    const text =
      'event: done\ndata: {"conversation_id": "c1",\ndata:  "message_id": "m1"}\n\n';
    const { events } = await collect(text);
    assert.equal(events.length, 1);
    assert.equal(events[0].name, 'done');
    assert.equal(events[0].data.message_id, 'm1');
  });

  it('坏 JSON 的 data 被静默忽略，不影响后续事件', async () => {
    const text =
      event('start', { conversation_id: 'c' }) +
      'event: done\ndata: {坏掉的JSON\n\n' +
      event('done', { conversation_id: 'c', message_id: 'good' });
    const { events } = await collect(text);
    assert.deepEqual(
      events.map((e) => e.name),
      ['start', 'done'],
    );
    assert.equal(events[1].data.message_id, 'good');
  });

  it('error 事件正常派发', async () => {
    const text =
      event('start', { conversation_id: 'c' }) +
      event('error', { message: '回答生成失败：模型不可用' }) +
      '';
    const { events } = await collect(text);
    assert.deepEqual(
      events.map((e) => e.name),
      ['start', 'error'],
    );
    assert.equal(events[1].data.message, '回答生成失败：模型不可用');
  });

  it('HTTP 非 2xx 抛出错误（由调用方进入兜底恢复）', async () => {
    const { events, error } = await collect('', { status: 500 });
    assert.deepEqual(events, []);
    assert.ok(error);
    assert.match(error.message, /HTTP 500/);
  });

  it('流最后一个事件缺尾随空行时仍被补派发', async () => {
    // 真实后端总是以 \n\n 结尾；此处模拟连接尾部分块缺失的极端情况
    const text = event('start', { conversation_id: 'c' }) + 'event: done\ndata: {"conversation_id":"c","message_id":"m9"}';
    const { events } = await collect(text);
    assert.deepEqual(
      events.map((e) => e.name),
      ['start', 'done'],
    );
    assert.equal(events[1].data.message_id, 'm9');
  });

  it('缺少 event 行时不派发任何回调', async () => {
    const text = 'data: {"message":"孤立 data"}\n\n' + event('done', { conversation_id: 'c', message_id: 'm' });
    const { events } = await collect(text);
    assert.deepEqual(
      events.map((e) => e.name),
      ['done'],
    );
  });

  it('未注册的回调被安全忽略', async () => {
    mockSse(event('done', { conversation_id: 'c', message_id: 'm' }));
    // 只注册部分 handler，不应抛错
    await assert.doesNotReject(() =>
      streamSSE('/x', {}, { onDelta: () => undefined }),
    );
  });
});
