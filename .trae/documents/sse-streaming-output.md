# SSE 流式输出实现计划

## 一、Summary（目标）

根据 PRD.md 第 3.2 节"支持流式输出（SSE），提供打字机式交互体验"与第 5.1 节对话主界面要求，实现 RAG 问答的流式输出：

- **后端**：新增 `POST /chat/ask-stream` 端点，使用 FastAPI `StreamingResponse` 返回 `text/event-stream`；LLM 启用 `stream: true` 逐 chunk 推送答案；检索命中作为引用来源在生成前推送，实现"引用卡片实时展示"。
- **前端**：新建 `useSSE.ts` hook，用 `fetch` + `ReadableStream` 解析 SSE 文本流（POST 无法用 EventSource）；`chatStore.sendMessage` 改为流式接收，助手消息占位后逐 chunk 追加内容形成打字机效果，引用卡片实时渲染。

## 二、Current State Analysis（现状分析）

| 文件 | 现状 | 改造点 |
| :--- | :--- | :--- |
| `backend/src/infrastructure/llm.py` | `OpenAICompatibleLLM.chat()` 用 `httpx.AsyncClient.post` 一次性返回完整字符串；`BaseLLM` 抽象只有 `chat` | 新增 `chat_stream()` 异步生成器，`httpx` stream 模式解析 `data:` 行，yield `delta.content` |
| `backend/src/application/rag_service.py` | `ask()` 编排会话+持久化+缓存；`retrieve_and_answer()` 检索+生成（不持久化，供评估复用）；`_build_citations()` 在完整答案上解析 `[citation: n, p]` | 新增 `ask_stream()` 异步生成器：编排会话+持久化用户消息→检索→推送 citations→流式生成→持久化助手消息→推送 done |
| `backend/src/api/routes/chat.py` | `POST /chat/ask` 返回 `ChatAnswerResponse` JSON | 新增 `POST /ask-stream`，返回 `StreamingResponse`；保留 `/ask` 供回退/评估兼容 |
| `frontend/src/services/api.ts` | `askQuestion()` 用 axios POST 获取完整答案 | 新增 `streamAskQuestion()` 返回 `AsyncGenerator`（或直接在 useSSE 内实现 fetch） |
| `frontend/src/stores/chatStore.ts` | `sendMessage()` 调 `askQuestion()`，一次性拿完整答案追加消息 | 改为调用 `useSSE` 流程：占位→逐 chunk 追加→设置 citations→完成 |
| `frontend/src/components/chat/MessageItem.tsx` | `renderAnswer()` 已能解析 `[citation: n, p]` 标记渲染 chip；`citations.length>0` 时渲染 Collapse 卡片 | 几乎无需改：随 content 增量更新自动重渲染即打字机效果；仅当 content 为空且正在流式时显示光标 |
| `frontend/src/app/chat/page.tsx` | `sending && "正在思考…"` 指示器 | 调整：发送后到首个 delta 之间显示"正在思考…"，首个 delta 到达后由打字机内容替代 |
| `frontend/src/hooks/` | **不存在**（TECH_DESIGN 规划中） | 新建 `useSSE.ts` |

**关键约束（来自 AGENTS.md / TECH_DESIGN.md）**：
- System Prompt 强制"仅根据资料回答，找不到就说不知道"（防幻觉）—— 保留不动。
- 所有检索强制 `kb_id` 过滤 —— 流式检索路径同样带 `kb_ids`。
- 引用标注 `[citation: 编号, 页码]` —— 流式 chunks 中保留该标记，`renderAnswer` 增量解析。
- `kb_ids` 为空返回 422 —— 流式端点同样校验。

## 三、SSE 事件协议（前后端约定）

文本流格式（每事件两行 + 空行分隔）：
```
event: start
data: {"conversation_id":"..."}

event: citations
data: {"citations":[{...}]}

event: delta
data: {"content":"chunk文本"}

event: done
data: {"conversation_id":"...","message_id":"..."}

event: error
data: {"message":"错误描述"}
```

**事件顺序**：`start` → `citations`（检索完成后立即推送，实现实时卡片）→ `delta`×N（LLM 流式）→ `done`。出错时 `error` 替代 `done`。

**引用来源策略**：`citations` 事件推送检索命中的全部 hits（即 Rerank 精排后的 Top-N），格式与现有 `Citation` 模型一致（`source_index` 1-based）。前端据此渲染卡片，答案中的 `[citation: n]` chip 立即可点击。`done` 后持久化到 DB 的 `citations.sources` 同样为全部 hits（保证历史回看与流式一致）。这与非流式 `/ask` 的"过滤为被引来源"略有差异，但流式场景下"全部来源实时展示"更符合 PRD"引用卡片实时展示"诉求；`/ask` 保留原行为不动。

## 四、Proposed Changes（详细改动）

### 后端

#### 4.1 `backend/src/infrastructure/llm.py`

**What**：为 `BaseLLM` 增加流式生成方法，`OpenAICompatibleLLM` 与 `MockLLM` 分别实现。

**How**：
- `BaseLLM` 增加抽象方法：
  ```python
  from collections.abc import AsyncIterator

  @abstractmethod
  async def chat_stream(self, messages: list[Message]) -> AsyncIterator[str]:
      """流式生成：逐 chunk yield 答案文本片段。"""
  ```
- `OpenAICompatibleLLM.chat_stream`：在 `payload` 中加 `"stream": True`，用 `httpx.AsyncClient` 的 `client.stream("POST", url, json=payload, headers=headers)` 打开流；`async for line in response.aiter_lines():` 解析 `data:` 前缀；跳过 `data: [DONE]`；其余行 `json.loads` 后取 `choices[0]["delta"]["content"]`（可能为 None，跳过）；非空则 `yield`。错误处理沿用 `LLMError`。复用 `settings.LLM_TIMEOUT_SECONDS`（流式下作为 connect/read 总超时）。
- `MockLLM.chat_stream`：复用现有 `chat()` 逻辑生成完整 mock 答案，然后按固定步长（如每 4 个字符一段）切片 `yield`，模拟流式；段间 `await asyncio.sleep(0.02)` 增强打字机观感。
- 保留 `chat()` 不变（`/ask` 与评估路径仍用）。

#### 4.2 `backend/src/application/rag_service.py`

**What**：新增 `ask_stream()` 异步生成器，编排"会话管理 + 检索 + 流式生成 + 持久化"，yield SSE 事件 dict。

**How**：
- 新增方法签名：
  ```python
  from collections.abc import AsyncIterator

  async def ask_stream(
      self,
      kb_ids: list[uuid.UUID],
      question: str,
      conversation_id: uuid.UUID | None = None,
  ) -> AsyncIterator[dict]:
      """流式 RAG 问答：yield SSE 事件 dict（{"event":..., "data":...}）。
      编排同 ask()，但生成阶段流式推送；持久化在生成完成后一次性完成。"""
  ```
- 实现步骤（复用现有私有方法）：
  1. `conversation = await self._ensure_conversation(kb_ids, question, conversation_id)`
  2. `yield {"event": "start", "data": {"conversation_id": str(conversation.id)}}`
  3. `history, _ = await self._load_history(conversation.id)`
  4. 持久化用户消息（独立事务，同 `ask()` 现有逻辑）
  5. **检索阶段**：复刻 `retrieve_and_answer` 的 HyDE → 混合检索 → RRF → Rerank → `_enrich_hits_with_doc_name` 逻辑（抽取为内部 `_retrieve()` 私有方法以避免重复，`retrieve_and_answer` 与 `ask_stream` 共用）。检索失败抛 `AppException(422)`（在端点层由异常处理器返回 422，不进入流）。
  6. 构造 citations：调用新私有方法 `_hits_to_citations(hits)`（即 `_build_citations` 的"全部 hits"版本，不按标记过滤，`source_index = i+1`）。
  7. `yield {"event": "citations", "data": {"citations": citations}}`
  8. 构造 `user_prompt = self._build_user_prompt(question, hits)`，`messages = [system, *history, user]`
  9. **流式生成**：
     ```python
     answer_parts: list[str] = []
     try:
         async for chunk in self.llm.chat_stream(messages):
             answer_parts.append(chunk)
             yield {"event": "delta", "data": {"content": chunk}}
     except LLMError as exc:
         logger.error(f"RAG 流式生成失败: {exc}")
         yield {"event": "error", "data": {"message": f"回答生成失败：{exc}"}}
         return
     answer = "".join(answer_parts).strip() or "（模型未返回内容，请重试）"
     ```
  10. 持久化助手消息：`Message(conversation_id=..., role="assistant", content=answer, citations={"sources": citations})` → `db.add` → `commit` → `refresh`
  11. Redis 缓存追加 user/assistant（同 `ask()`）
  12. `yield {"event": "done", "data": {"conversation_id": str(conversation.id), "message_id": str(assistant.id)}}`
- **重构 `_retrieve()`**：把 `retrieve_and_answer` 中 HyDE→检索→RRF→Rerank→`_enrich_hits_with_doc_name` 抽成 `async def _retrieve(self, kb_ids, question) -> list[dict]`，两个方法复用，避免逻辑漂移。`retrieve_and_answer` 调 `_retrieve` 后接 `_build_user_prompt` + `self.llm.chat`。
- **新增 `_hits_to_citations(hits)`**：返回全部 hits 的 citation dict 列表（`source_index = i+1`，其余字段同 `_build_citations`）。

#### 4.3 `backend/src/api/routes/chat.py`

**What**：新增 `POST /chat/ask-stream` 端点。

**How**：
```python
import json
from fastapi.responses import StreamingResponse

@router.post("/ask-stream")
async def ask_stream(payload: ChatAskRequest, db: AsyncSession = Depends(get_db)):
    """RAG 问答（SSE 流式）：检索 → 推送引用 → 逐 chunk 流式生成 → 持久化。

    事件协议：start / citations / delta / done / error（见计划第三节）。
    """
    from src.core.exceptions import AppException
    if not payload.kb_ids:
        raise AppException(422, "kb_ids 不能为空：必须指定检索的知识库范围")

    service = RagService(db)

    async def event_stream():
        try:
            async for evt in service.ask_stream(
                kb_ids=payload.kb_ids,
                question=payload.question.strip(),
                conversation_id=payload.conversation_id,
            ):
                data = json.dumps(evt["data"], ensure_ascii=False)
                yield f"event: {evt['event']}\ndata: {data}\n\n"
        except Exception as exc:  # 兜底，避免流中断无提示
            logger.exception(f"流式问答异常: {exc}")
            err = json.dumps({"message": str(exc)}, ensure_ascii=False)
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx 不缓冲，保证实时推送
        },
    )
```
- 文件头注释更新：去掉"SSE 流式输出后续迭代"字样。
- 保留 `POST /chat/ask`（评估不直接调它，但作为非流式回退）。
- 新增 `from loguru import logger` 导入。

### 前端

#### 4.4 `frontend/src/hooks/useSSE.ts`（新建）

**What**：封装 `fetch` + `ReadableStream` 解析 SSE 文本流的通用 hook。

**How**：
```typescript
'use client';
import { useCallback } from 'react';

export interface SSEHandlers {
  onStart?: (data: { conversation_id: string }) => void;
  onCitations?: (data: { citations: Citation[] }) => void;
  onDelta?: (data: { content: string }) => void;
  onDone?: (data: { conversation_id: string; message_id: string }) => void;
  onError?: (data: { message: string }) => void;
}

/** 用 fetch POST + ReadableStream 解析 SSE 文本流（EventSource 仅支持 GET，故用 fetch）。 */
export function useSSE() {
  return useCallback(async (url: string, body: unknown, handlers: SSEHandlers) => {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`SSE 请求失败：${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let currentEvent = '';
    const dispatch = (event: string, dataStr: string) => {
      if (!dataStr) return;
      const data = JSON.parse(dataStr);
      switch (event) {
        case 'start': handlers.onStart?.(data); break;
        case 'citations': handlers.onCitations?.(data); break;
        case 'delta': handlers.onDelta?.(data); break;
        case 'done': handlers.onDone?.(data); break;
        case 'error': handlers.onError?.(data); break;
      }
    };
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? ''; // 保留未完整行
      let pendingData = '';
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          pendingData += line.slice(6);
        } else if (line === '') {
          // 事件分隔空行：派发
          if (currentEvent && pendingData) dispatch(currentEvent, pendingData);
          currentEvent = '';
          pendingData = '';
        }
      }
    }
  }, []);
}
```
- 注：SSE `data` 多行时需拼接（这里按单行 data 简化，因后端每事件 data 单行 JSON）。如果 `data` 跨多行，逐行累加到 `pendingData` 后于空行派发——上面已按此处理。

#### 4.5 `frontend/src/stores/chatStore.ts`

**What**：重写 `sendMessage` 使用 SSE 流式接收。

**How**：
- 引入 `useSSE` 与 `Citation` 类型。
- `sendMessage` 改造：
  ```typescript
  sendMessage: async (question: string) => {
    const { selectedKbId, currentConversationId } = get();
    if (!selectedKbId || get().sending) return;

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(), role: 'user', content: question,
    };
    // 预声明助手消息占位 id，便于增量更新
    const assistantId = crypto.randomUUID();
    set((s) => ({
      messages: [...s.messages, userMsg, { id: assistantId, role: 'assistant', content: '', citations: [] }],
      sending: true,
    }));

    const sse = useSSE(); // 注意：hook 不能在普通函数内调，改用模块级工具函数（见下）
    ...
  ```
  **重要修正**：`useSSE` 是 React hook，不能在 Zustand 的 `sendMessage` 内调用。改方案：把 `useSSE.ts` 导出一个**普通工具函数** `streamSSE(url, body, handlers)` 而非 hook（或同时导出 hook 与函数）。Zustand store 直接调 `streamSSE(...)`。`useSSE` hook 保留供组件需要时使用。**采纳：导出普通函数 `streamSSE`**，文件名仍为 `useSSE.ts`（含 hook 版本 + 工具函数）。

  ```typescript
  sendMessage: async (question) => {
    const { selectedKbId, currentConversationId } = get();
    if (!selectedKbId || get().sending) return;

    const userMsg: ChatMessage = { id: crypto.randomUUID(), role: 'user', content: question };
    const assistantId = crypto.randomUUID();
    set((s) => ({
      messages: [...s.messages, userMsg, { id: assistantId, role: 'assistant', content: '', citations: [] }],
      sending: true,
    }));

    let bufferedCitations: Citation[] = [];
    try {
      await streamSSE('/api/v1/chat/ask-stream',
        { question, kb_ids: [selectedKbId], conversation_id: currentConversationId },
        {
          onStart: ({ conversation_id }) => {
            set({ currentConversationId: conversation_id });
          },
          onCitations: ({ citations }) => {
            bufferedCitations = citations;
            // 此时助手消息可能尚未显示 delta，但卡片可先挂上
            set((s) => ({
              messages: s.messages.map((m) => m.id === assistantId ? { ...m, citations } : m),
            }));
          },
          onDelta: ({ content }) => {
            set((s) => ({
              messages: s.messages.map((m) => m.id === assistantId
                ? { ...m, content: m.content + content } : m),
            }));
          },
          onDone: ({ message_id, conversation_id }) => {
            set((s) => ({
              messages: s.messages.map((m) => m.id === assistantId
                ? { ...m, id: message_id || m.id } : m),
              currentConversationId: conversation_id,
              sending: false,
            }));
            void get().loadConversations();
          },
          onError: ({ message }) => {
            set((s) => ({
              messages: s.messages.map((m) => m.id === assistantId
                ? { ...m, content: m.content || `生成失败：${message}` } : m),
              sending: false,
            }));
          },
        });
    } catch (e) {
      set((s) => ({
        messages: s.messages.map((m) => m.id === assistantId
          ? { ...m, content: m.content || '请求失败，请稍后重试。' } : m),
        sending: false,
      }));
    }
  },
  ```
- 移除对 `askQuestion` 的导入依赖（如该函数不再被引用可删，但保留无害）。

#### 4.6 `frontend/src/components/chat/MessageItem.tsx`

**What**：极小改动——流式进行中（content 为空且为本条流式）显示输入光标提示。

**How**：
- 不需要新增 streaming 标记：当 `content === ''` 且 role 为 assistant 时，渲染"正在输入…"或闪烁光标 `▋` 即可。改 `renderAnswer` 调用处：
  ```tsx
  {message.content
    ? renderAnswer(message.content, citations, (n) => setActiveKey([String(n)]), activeKey[0] ? parseInt(activeKey[0], 10) : null)
    : <span style={{ color: '#8c8c8c' }}>正在输入…</span>}
  ```
- 引用卡片部分已随 `citations` 实时更新自动渲染，无需改。

#### 4.7 `frontend/src/app/chat/page.tsx`

**What**：调整"正在思考…"指示器逻辑。

**How**：
- 当前：`sending && <div>正在思考…</div>`。流式下，助手占位消息已存在（content 初始为空，MessageItem 显示"正在输入…"），二者重复。
- 改为：仅当 `sending` 且最后一条助手消息 `content` 为空时显示"正在检索知识库…"，否则由 MessageItem 的打字机内容接管。
  ```tsx
  {sending && messages[messages.length - 1]?.role === 'assistant'
    && messages[messages.length - 1]?.content === '' && (
    <div style={{ color: '#8c8c8c', padding: '8px 16px', fontSize: 13 }}>
      正在检索知识库…
    </div>
  )}
  ```
  注：占位助手消息已进 messages 数组，上面判断其 content 为空即"检索阶段"。首个 delta 到达后 content 非空，提示消失，打字机接管。

## 五、Assumptions & Decisions（假设与决策）

1. **传输方式**：采用 `fetch` + `ReadableStream`（用户已确认）。EventSource 不支持 POST，故 hook 内部用 fetch；文件仍命名为 `useSSE.ts` 并同时导出普通函数 `streamSSE` 供 Zustand store 调用（store 非 React 组件，不能直接用 hook）。
2. **引用来源策略**：流式推送全部检索命中（`citations` 事件在生成前），实现 PRD"引用卡片实时展示"；DB 持久化同全部命中。非流式 `/chat/ask` 的"过滤为被引来源"行为保留不变。
3. **保留 `/chat/ask`**：作为非流式回退与评估兼容路径（评估直接用 `retrieve_and_answer`，不经 HTTP）。
4. **MockLLM 流式**：按 4 字符切片 + 20ms 间隔，模拟打字机，便于无 GPU 环境验证。
5. **DB 会话生命周期**：`StreamingResponse` 期间 `get_db` 依赖保持活跃（FastAPI 生成器响应中依赖不提前释放），满足生成后持久化需求。
6. **System Prompt 不变**：防幻觉约束在流式路径同样生效（messages 构造同 `retrieve_and_answer`）。
7. **`[citation: n, p]` 标记跨 chunk**：LLM 可能将一个标记拆到多个 chunk。前端 `renderAnswer` 每次对完整累积 content 重新解析（store 追加后整体重渲染），故跨 chunk 标记在累积完整后能正确匹配；流式过程中部分标记可能暂不显示为 chip，待完整后自动补全——可接受。

## 六、Verification（验证步骤）

### 后端
1. **单元测试**：新增 `backend/tests/test_chat_stream.py`，MockLLM 流式切片正确；`ask_stream` 产出事件序列 `start → citations → delta* → done`；`kb_ids` 为空抛 422；检索失败走 error 事件。
2. **接口手测**：`curl -N -X POST http://localhost:8000/api/v1/chat/ask-stream -H "Content-Type: application/json" -d '{"question":"...","kb_ids":["..."]}'`，观察逐 chunk 输出与 `event:` 行。
3. **pytest 全量**：`cd backend && pytest` 通过（含新增 + 既有 66 例）。
4. **lint**：`ruff check src && black --check src && isort --check src` 通过。

### 前端
5. **类型检查**：`cd frontend && npx tsc --noEmit` 通过。
6. **lint**：`npm run lint` 通过。
7. **浏览器手测**：访问 `http://localhost:3000/chat`，选知识库提问，验证：答案逐字出现（打字机）；引用卡片在答案开始前/同时出现；chip 可点击展开；多轮上下文记忆；刷新会话历史完整。
8. **后端热重载**：确认后端进程加载新 `chat.py`/`rag_service.py`/`llm.py`（如未开 `--reload` 需重启）。

### 集成
9. 端到端：前端提问 → SSE 流到达 → 打字机 + 卡片 → 完成后会话列表刷新 → 切回历史会话可见完整答案与引用。
