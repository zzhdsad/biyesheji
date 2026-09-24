# BATCH_5_FIX_REPORT.md

> 第二轮 Batch 5（前端）修复报告
> 依据：`BUG_AUDIT_REPORT.md`　生成时间：2026-09-24
> 范围：**仅 Batch 5 的 13 个 Bug**（BUG-013、048、049、050、051、052、053、054、055、056、057、058、062），未触碰 Batch 6 内容。本批未改后端。

---

## 1. 总览

| 项 | 结果 |
|---|---|
| 本批计划修复 | 13 个（BUG-013、048、049、050、051、052、053、054、055、056、057、058、062） |
| 已修复 | **12 个** |
| 复核后无需修复 | **1 个**（BUG-055，见 §6） |
| 新增回归测试 | **13 条**（`requestSeq.test.ts` 4 + `evidence.test.ts` 4 + `chatStore.test.ts` 3 + `useSSE.test.ts` 2） |
| 修改文件 | 15 个（新增 2：`utils/requestSeq.ts`、`hooks/useRequestSeq.ts`；测试 4 个） |
| `npm test`（前端） | **77 passed**（Batch 4 后基线 64 → **+13，无回归**） |
| `tsc --noEmit` | **0 error** |
| `npm run lint` | 5 warnings（**与改动前基线完全一致，无新增**；均为既有 `useEffect` 依赖告警） |
| `npm run build` | **成功**（17 路由 + middleware，构建产物正常） |
| 后端改动 | 无（本批为前端批次） |

### 本批的两个"补缺口"判断（按 AGENTS §2/§3 最小改动原则）

| 判断点 | 处理 |
|---|---|
| BUG-057 反馈入口缺失 | 属**补缺口**而非新增功能：后端 `/feedbacks` 已实现、PRD §3.6 要求点赞/纠错入口，组件已存在但无引用 → 直接接入助手消息 |
| BUG-048 列表竞态的修复方式 | 只做**状态层守卫**（丢弃过期响应），不引入 AbortController 取消在飞请求：axios 侧无取消机制，改动面会扩散到所有 service；守卫已能消除"旧响应覆盖新数据"的全部可见症状 |

---

## 2. 逐 Bug 修复明细

### BUG-013（P1）chatStore 会话切换/删除竞态

- **根因**：`selectConversation` 的 `fetchMessages` 没有任何序号或取消机制：① 快速切会话时旧慢响应后到 → 消息与选中会话不一致；② 流式期间切会话，`onStart` / `onDone` 把 `currentConversationId` 强行写回旧流会话；③ 删除会话后在飞的消息请求仍 `set({ messages })`，让已删会话的历史"复活"。
- **修复**：新增会话视图序号守卫（复用 `utils/requestSeq.ts`）：
  - `selectConversation` 用 `begin()/isLatest()` 丢弃过期响应；
  - `newConversation` / `deleteConversation` / `deleteAllConversations` 调用 `invalidate()` 作废在飞加载；
  - `sendMessage` 在发起时快照 `current()`，流式回调写入 `currentConversationId` 前校验归属（用户已切走则不写回）。
- **回归测试**：`chatStore.test.ts` 新增 3 条（切会话慢响应、删除后不复活、流式期间切走）。**已验证：回退修复后这 3 条全部失败**（`pass 74 / fail 3`），修复后通过。

### BUG-048（P2）6 个列表页无请求序号/取消

- **根因**：① `theories` / `literatures` 非第 1 页点查询时 `setCurrent(1)` 触发 effect 又发一次请求，同时本次调用用的还是**旧页码**（双发 + 数据错配）；② 快速翻页/改条件时旧慢响应覆盖新数据与 `total`；③ `evaluation` 串行 `await` 三个接口且无作废，快速切 KB 时旧 KB 结果覆盖新 KB。
- **修复**：
  - 新增 `utils/requestSeq.ts`（纯函数工厂）+ `hooks/useRequestSeq.ts`（`useRef` 包一层），语义：`begin()` 发号、`isLatest(id)` 判最新、`invalidate()` 全作废、`current()` 快照。
  - 6 个页面接入守卫：`theories`、`literatures`、`herbs`、`prescriptions`、`evaluation`、`kb/resources`——过期响应直接 return，错误提示与 loading 复位也只在最新请求上生效。
  - `theories` / `literatures` 的 `onSearch` / `onReset` 改为与 `herbs` 一致的写法：已在第 1 页则**显式带条件**查询一次，否则只切页码由 effect 加载（`theories` 同时去掉了 `setTimeout(0)` 的 hack）。
  - `evaluation.handleKbChange` 改为并发 `Promise.all` + 单次序号校验。
- **回归测试**：`requestSeq.test.ts` 4 条（递增/作废/快照/多次作废）。

### BUG-049（P2）kb/resources 的 columns 闭包过期

- **根因**：`columns` 的 `useMemo` 依赖仅 `[kbId]`，闭包捕获的是首次渲染的 `onUnmount` → 后者又捕获旧的 `loadResources`（旧 `filterType`），切换筛选后点"卸载"会用旧条件刷新列表。
- **修复**：`onUnmount` 用 `useCallback` 稳定引用（依赖 `[kbId, message, loadResources]`），`columns` 依赖改为 `[onUnmount]`。
- **回归测试**：无单独用例（React 组件依赖数组的正确性由 `npm run lint` 的 `react-hooks/exhaustive-deps` 守卫：修改后无告警）。

### BUG-050（P1，安全）创建用户后残留上一个用户的初始密码

- **根因**：`onCreate` 成功即 `setCreateOpen(false)`，但 `createdPwd` 不清 → 既没机会展示本次初始密码，下次打开弹窗又会显示**上一个用户的随机密码**（凭证错配 + 泄露）。
- **修复**：① 打开弹窗前 `setCreatedPwd(null)`；② 创建成功后**保留弹窗**展示本次初始密码，OK 按钮转为"关闭"（`okText` 切换 + 隐藏取消按钮），关闭/取消一律清 `createdPwd`。
- **回归测试**：无（弹窗状态机属 UI 行为，仓库无 jsdom/RTL，见 §6）。

### BUG-051（P2）审计详情页 `Object.keys(null)` 崩溃

- **根因**：`detail` 列 `render` 直接 `Object.keys(v).length`，后端 JSONB 为 null 或历史脏数据缺失时整页 TypeError。
- **修复**：`v && Object.keys(v).length > 0 ? JSON.stringify(v) : '-'`，渲染入参类型改为 `Record<string, unknown> | null | undefined`。
- **回归测试**：无（表格列渲染属 UI 行为）。

### BUG-052（P2）提问输入框无限长 vs 后端 2000

- **根因**：`Input.TextArea` 无 `maxLength`，超长提交后才拿到 422。
- **修复**：`maxLength={QUESTION_MAX_LENGTH}`（常量 2000，注释标注与后端 `ChatAskRequest.question.max_length` 对齐）+ `showCount`，并在 `handleSend` 提交前二次校验（超限时 `message.warning` 提示当前字数，不发起请求）。
- **回归测试**：无（输入交互属 UI 行为）；契约本身由 `tests/api-contract.test.ts` 守卫。

### BUG-053（P3）`renderAnswer(message.content)` 未兜底

- **根因**：历史脏数据 `content = null` 时 `rest.length` 直接抛错 → 聊天页白屏。
- **修复**：`renderAnswer(message.content ?? '', onChip)`。
- **回归测试**：无（组件渲染属 UI 行为）。

### BUG-054（P3）方剂页 `ingredients` / `tags` 未兜底

- **根因**：`[...row.ingredients]`、`editTarget.tags.map(...)`、`[...detail.ingredients]` 未 `?? []`（herbs 页同类代码已处理）→ 后端返回 null 时表格/Drawer/编辑弹窗崩溃。
- **修复**：三处统一 `?? []`。
- **回归测试**：无（UI 渲染）。

### BUG-055（P3）上传未选知识库时永久 uploading —— **复核后无需修复**

- **复核结论**：`app/documents/page.tsx:132-137` 当前代码 **已调用** `onError?.(new Error('未选择知识库'))`（`git show HEAD` 与工作区一致，非本批改动）；且 `<Upload disabled={!selectedKbId} showUploadList>` 在未选知识库时按钮本就禁用，该分支实际不可达。审计描述与代码现状不符，故**不做改动**，保留记录以免误改。

### BUG-056（P3）新建方剂后看不到新建记录

- **根因**：新建成功后原地 `loadPrescriptions()` 刷新当前页，而新记录落在第一页；删除后页码越界也无校核。
- **修复**：① 新建成功且当前不在第 1 页时 `setCurrent(1)`（由 effect 加载），在第 1 页则直接刷新；② `loadPrescriptions` 内加页码越界校核：返回空且 `pg > 1` 时回退一页（删除/筛选导致越界同样生效）。
- **回归测试**：无（分页交互属 UI 行为）。

### BUG-057（P3）FeedbackButtons 全项目无引用

- **根因**：组件已实现但从未被引用 → 后端 `/feedbacks` 无 UI 入口，PRD 要求的点赞/纠错收集形同虚设。
- **修复**：在 `MessageItem` 的助手消息下方挂载 `<FeedbackButtons messageId={message.id} />`（组件自带惰性拉取：hover/点击时才请求，流式期间的临时 id 请求失败静默忽略）。与后端 BUG-038 呼应：仅助手消息可反馈。
- **回归测试**：无（组件挂载属 UI 行为）。

### BUG-058（P3）引用 chip 编号与面板 key 无映射

- **根因**：chip 点击 `setActiveKey([String(index)])`，而 Collapse 的 key 是证据 `source_index`；模型引用越界编号（只有 3 条来源却写 `[citation: 5]`）时点不出任何面板，且再次点击同一 chip 因 key 相同无法收起。
- **修复**：新增 `utils/evidence.ts` 的 `resolveCitationKey(chipIndex, availableSourceIndexes)`——命中返回 `String(n)`，越界/非法返回 `null`；`MessageItem` 用它在点击时解析目标 key：解析不到即忽略（不展开、不报错），解析到则**切换**（已展开则收起）。
- **回归测试**：`evidence.test.ts` 新增 4 条（命中、越界、空集合/NaN、编号不连续时精确匹配）。

### BUG-062（P3）SSE 绕过 401 拦截器

- **根因**：`streamSSE` 用原生 fetch，不过 `api.ts` 的 axios 拦截器 → token 过期只抛"请求失败"，不清 token、不跳转，用户一直停在失效页面重试。
- **修复**：`token.ts` 新增 `handleUnauthorized()`（清 token + `window.location` 硬跳 `/login?redirect=...`，登录页自身 401 不跳），`api.ts` 拦截器与 `useSSE.ts` **共用**该实现（消除两份重复逻辑）；SSE 侧 401 时先处理再抛"登录已过期，请重新登录"。
- **回归测试**：`useSSE.test.ts` 新增 2 条（清 token + 跳转 URL；登录页不跳转）。

---

## 3. 修改文件清单

| 文件 | 改动要点 |
|---|---|
| `src/utils/requestSeq.ts`（新增） | 请求序号守卫工厂（纯函数，可单测） |
| `src/hooks/useRequestSeq.ts`（新增） | 组件内 `useRef` 版本 |
| `src/stores/chatStore.ts` | BUG-013：会话视图序号守卫（切换/新建/删除作废；流式回调校验归属） |
| `src/hooks/useSSE.ts` | BUG-062：401 走 `handleUnauthorized` |
| `src/services/token.ts` | BUG-062：新增 `handleUnauthorized()` |
| `src/services/api.ts` | BUG-062：401 拦截器改为复用 `handleUnauthorized`（去重） |
| `src/utils/evidence.ts` | BUG-058：新增 `resolveCitationKey` |
| `src/components/chat/MessageItem.tsx` | BUG-053 内容兜底、BUG-057 挂载反馈入口、BUG-058 编号映射 + 折叠切换 |
| `src/app/chat/page.tsx` | BUG-052：`maxLength` + `showCount` + 提交前校验 |
| `src/app/audit/page.tsx` | BUG-051：`detail` 空值兜底 |
| `src/app/users/page.tsx` | BUG-050：打开弹窗清密码、成功后展示本次密码、按钮转"关闭" |
| `src/app/prescriptions/page.tsx` | BUG-054 三处 `?? []`、BUG-056 新建回第一页 + 页码越界回退、BUG-048 守卫 |
| `src/app/theories/page.tsx` | BUG-048：守卫 + 修双发请求（`loadTheories` 支持显式条件） |
| `src/app/literatures/page.tsx` | BUG-048：守卫 + 修双发请求 |
| `src/app/herbs/page.tsx` | BUG-048：守卫 |
| `src/app/evaluation/page.tsx` | BUG-048：并发加载 + 序号守卫 |
| `src/app/kb/resources/page.tsx` | BUG-048 守卫 + BUG-049 `columns` 依赖修正 |
| `src/utils/requestSeq.test.ts`（新增） | 4 条 |
| `src/utils/evidence.test.ts` | +4 条（BUG-058） |
| `src/stores/chatStore.test.ts` | +3 条（BUG-013） |
| `src/hooks/useSSE.test.ts` | +2 条（BUG-062） |

---

## 4. 向后兼容性说明

| 兼容对象 | 说明 |
|---|---|
| SSE 事件协议 | 未改事件名/顺序/字段（BUG-062 只在 HTTP 401 分支新增行为） |
| API 契约 | 未改任何请求/响应结构（前端只做入参限长与空值兜底） |
| 列表页交互 | 默认值与旧行为一致；守卫只丢弃**已过期**的响应，正常路径无变化 |
| 创建用户弹窗 | 新增"展示本次初始密码 + 关闭"步骤（旧行为是创建即关闭、密码看不见） |
| 反馈入口 | 仅新增 UI 挂载，后端接口与字段未变 |
| 新增工具模块 | `utils/requestSeq.ts` / `hooks/useRequestSeq.ts` 为内部实现，不对外暴露 |

---

## 5. 需要知晓的行为变更（对使用方）

| 场景 | 变更前 | 变更后 |
|---|---|---|
| 快速切换会话 / 删除会话 | 慢响应覆盖新会话消息；已删会话消息复活 | 过期响应丢弃，消息与选中会话始终一致 |
| 流式期间切走会话 | `currentConversationId` 被旧流回调拉回 | 不写回；流继续跑但无副作用 |
| 列表页快速翻页/改条件 | 旧响应覆盖新数据与总数 | 只保留最新一次请求的结果 |
| theories/literatures 非第 1 页点查询 | 双发请求，且其中一次用旧页码 | 只发一次，条件与页码一致 |
| 评测页快速切知识库 | 旧 KB 的历史/运行/测试集覆盖新 KB | 并发加载 + 过期丢弃 |
| 创建用户 | 弹窗立刻关闭，初始密码既看不见又会残留到下次 | 展示本次初始密码，点"关闭"结束；打开时清除历史密码 |
| 提问超 2000 字 | 提交后返回 422 | 输入框限长 + 计数 + 提交前提示 |
| 审计详情为空 | 整页崩溃 | 显示 `-` |
| 方剂数据缺 ingredients/tags | 表格/Drawer 崩溃 | 按空数组渲染 |
| 新建方剂 | 停留在当前页，看不到新建记录 | 回到第一页；页码越界自动回退 |
| 点击引用 chip | 越界编号点不动、再点无法收起 | 越界忽略；命中则展开/收起切换 |
| 助手回答 | 无反馈入口 | 下方新增 👍/👎（可填纠错意见） |
| token 过期后发起问答 | 只提示"请求失败"，页面卡住 | 清 token 并跳转登录页（带 redirect） |

---

## 6. 遗留与待办

| 项 | 归属 | 说明 |
|---|---|---|
| BUG-055 | **复核后无需修复** | 代码已调用 `onError`，且未选知识库时上传按钮 `disabled`，审计描述与现状不符（详见 §2） |
| 无 DOM/E2E 保护 | Batch 6 / 质量批次 | 仓库无 jsdom / RTL / Playwright：BUG-050、051、052、053、054、056、057 的 UI 行为**只能人工验证**，未自动化；本批只在可抽取纯逻辑的部分（序号守卫、编号映射、store 竞态、401 处理）补了测试 |
| 在飞请求未真正取消 | 已知取舍 | BUG-048 只做状态守卫，未引入 AbortController；SSE 流无法中止（切走后仍在跑，但写入被守卫拦截，无副作用、无可见错误） |
| `taxonomy` 等未列出的列表页 | 不在 BUG-048 编号范围 | 如后续发现同类竞态，按同一守卫模式补齐即可 |
| BUG-059（`/kb` 无分页）、BUG-060（setTimeout 未清理）、BUG-061（cookie max-age 7 天 vs JWT 1 天） | Batch 6 | 本批未触碰 |
| 后端 | — | 本批未改后端；后端全量测试仍为 Batch 4 后的 **652 passed / 2 skipped** |
