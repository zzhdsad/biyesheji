# 鉴权模块实现计划

## 一、现状分析

### 已有
| 位置 | 已有内容 |
| :--- | :--- |
| `backend/src/domain/models.py:30-37` | User 表：id / email / username / hashed_password / role（member/admin） |
| `backend/src/core/config.py:28-30` | SECRET_KEY、ACCESS_TOKEN_EXPIRE_MINUTES（默认 1440 分钟=7 天） |
| `backend/src/core/exceptions.py` | AppException / NotFoundError / PermissionDeniedError |
| `backend/src/infrastructure/database.py:45-68` | init_db() 建表 + seed 默认 admin（hashed_password="not-set-yet" 占位） |
| `backend/src/main.py:49-52` | 只注册 api_router + health_router，无全局 auth 中间件 |
| `backend/src/api/routes/__init__.py:1-11` | documents/chat/knowledge_bases/evaluation 四个路由 |
| `frontend/src/services/api.ts:17-20` | axios 实例，**无** Authorization 拦截器、**无** 401 处理 |
| `frontend/src/stores/chatStore.ts` | 纯业务 store，**无** auth 状态 |
| `frontend/src/app/layout.tsx` | 根布局，**无** AuthProvider / 路由守卫 |
| `frontend/src/app/page.tsx` | 无条件 redirect('/chat') |

### 缺失
| 后端 | 前端 |
| :--- | :--- |
| PyJWT / passlib / bcrypt 未安装 | 无 auth store |
| 无 security.py（JWT + 密码哈希） | 无 axios 拦截器（自动带 token） |
| 无 deps.py（get_current_user 依赖注入） | 无 401→登录 跳转逻辑 |
| 无 auth.py 路由（/register /login /logout /me） | 无登录页 / 注册页 |
| api_router 无鉴权保护 | 无 request.state.user 读取逻辑 |

### 依赖库
需安装：`PyJWT passlib[bcrypt] bcrypt`

---

## 二、后端改动

### 2.1 安装依赖
```powershell
cd backend
.\.venv\Scripts\pip.exe install PyJWT passlib[bcrypt] bcrypt
```
更新 pyproject.toml 的 [project] 段添加依赖声明。

### 2.2 新建 `src/core/security.py`
职责：
- `pwd_context = CryptContext(schemes=["bcrypt"])`
- `hash_password(plain: str) -> str` — bcrypt 哈希
- `verify_password(plain: str, hashed: str) -> bool` — bcrypt 校验
- `create_access_token(subject: str | uuid.UUID, role: str, expires_delta: timedelta | None = None) -> str`
  - 使用 PyJWT `encode(payload, SECRET_KEY, algorithm="HS256")`
  - payload: `{"sub": str(user_id), "role": role, "exp": exp_ts, "iat": now_ts, "type": "access"}`
- `decode_access_token(token: str) -> dict` — PyJWT `decode(..., algorithms=["HS256"])`，过期或非法抛 JWTError → 上层 401

### 2.3 新建 `src/core/deps.py`
职责：
- `get_current_user(authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> User`
  - 无 `Authorization: Bearer xxx` → HTTPException 401 "未提供认证信息"
  - 格式校验、decode → 拿 sub(user_id) + role
  - 查 DB（select User where id = sub）
  - 用户不存在 / 被删除 → 401
  - 返回 User ORM 对象
- （可选）`get_optional_current_user(...) -> User | None` — 有 token 解析、无 token 不报错

### 2.4 新建 `src/api/routes/auth.py`
前缀 `/auth`，全部**免鉴权**（挂在 auth_public_router 下）。

| 端点 | 方法 | 请求体 | 响应 | 校验 |
| :--- | :--- | :--- | :--- | :--- |
| `/auth/register` | POST | `{ "email", "username", "password" }` | `{ "id", "email", "username", "role" }` | email/username 唯一；密码 ≥6 字符；返回 User Out（无 token，不自动登录） |
| `/auth/login` | POST | `{ "username_or_email", "password" }` | `{ "access_token", "token_type": "bearer", "expires_in": 604800, "user": { "id", "email", "username", "role" } }` | 查 User（email OR username），verify_password，生成 JWT |
| `/auth/logout` | POST | 无（需鉴权） | `{ "message": "ok" }` | 仅语义（前端删 token），DB 不做 session 表 |
| `/auth/me` | GET | Header Bearer | User Out | 返回当前用户信息 |

Pydantic schemas 放在同一文件底部（避免单独 schemas 目录）：
- `RegisterRequest(email=EmailStr, username=min_length(3), password=min_length(6))`
- `LoginRequest(username_or_email=str, password=str)`
- `TokenResponse(access_token=str, token_type="bearer", expires_in=int, user=UserOut)`
- `UserOut(id=UUID, email=EmailStr, username=str, role=str)`

### 2.5 修改 `src/api/routes/__init__.py`
```python
from src.api.routes import auth

# auth 免鉴权路由（独立父 router，不挂 dependencies）
auth_public_router = APIRouter()
auth_public_router.include_router(auth.router)  # /register, /login

# 业务路由 —— 统一挂鉴权依赖（父 router 层统一保护）
protected_router = APIRouter(dependencies=[Depends(get_current_user)])
protected_router.include_router(documents.router)
protected_router.include_router(chat.router)
protected_router.include_router(knowledge_bases.router)
protected_router.include_router(evaluation.router)

api_router = APIRouter()
api_router.include_router(auth_public_router)
api_router.include_router(protected_router)
```

> 决策点：`/auth/logout` 和 `/auth/me` 也需要鉴权，所以挂在 auth.router 内但这两个端点单独加 `Depends(get_current_user)`。auth_public_router 只包含 `/register` 和 `/login`。

### 2.6 修改 `src/main.py`
无需改动（api_router 已被替换为包含 auth_public + protected 的聚合 router）。

### 2.7 修改 `src/infrastructure/database.py` init_db()
默认 admin 改为 bcrypt 哈希，调用 `hash_password("admin123")`。在 import 阶段 import security 模块（注意不要循环依赖）。

### 2.8 修改 `src/core/config.py`
添加 `JWT_ALGORITHM: str = "HS256"` 配置项。（SECRET_KEY 和 ACCESS_TOKEN_EXPIRE_MINUTES 已有）

### 2.9 测试文件
新建 `backend/tests/test_auth.py`（pytest + httpx）：
- test_register_success
- test_register_duplicate_email_400
- test_register_password_too_short_422
- test_login_success_returns_token
- test_login_wrong_password_401
- test_login_unknown_user_401
- test_access_protected_endpoint_without_token_401
- test_access_protected_endpoint_with_token_200
- test_me_returns_current_user
- test_bad_token_401
- test_expired_token_401（可选，构造短过期 token）

---

## 三、前端改动

### 3.1 新建 `src/stores/userStore.ts`（zustand）
```ts
interface UserState {
  token: string | null;
  user: UserOut | null;
  loading: boolean;
  error: string | null;
  login: (usernameOrEmail: string, password: string) => Promise<boolean>;
  register: (email: string, username: string, password: string) => Promise<boolean>;
  logout: () => void;
  loadMe: () => Promise<void>;
  isAuthenticated: () => boolean;
}
```
- 初始化时从 localStorage 读 `kp_token` / `kp_user`
- login() → POST /auth/login → 存 token + user 到 state + localStorage
- register() → POST /auth/register → 不自动登录（与后端行为一致）
- logout() → 清 state + localStorage + redirect /login
- isAuthenticated() → `!!token`

### 3.2 修改 `src/services/api.ts`
axios 拦截器：
```ts
// 请求拦截器：自动带 Authorization
api.interceptors.request.use((config) => {
  const token = useUserStore.getState().token;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// 响应拦截器：401 自动跳登录
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      useUserStore.getState().logout();
      // Next.js 14 App Router：window.location.href 硬跳转（避免 SPA 路由守卫循环）
      if (typeof window !== 'undefined') {
        window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname);
      }
    }
    return Promise.reject(err);
  },
);
```

### 3.3 新建 `src/services/auth.ts`（或直接加到 api.ts）
```ts
export async function apiLogin(body: { username_or_email: string; password: string })
export async function apiRegister(body: { email: string; username: string; password: string })
export async function apiFetchMe(): Promise<UserOut>
```

### 3.4 新建 `src/middleware.ts`（Next.js 路由守卫）
App Router 用 middleware 做 SSR 侧保护：
```ts
import { NextRequest, NextResponse } from 'next/server';

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  const token = req.cookies.get('kp_token')?.value ??
                // localStorage 在 middleware 不可用，用 cookie 存同一份 token
                null;

  // 公开路由放行
  if (pathname.startsWith('/login') || pathname.startsWith('/register') ||
      pathname.startsWith('/health') || pathname.startsWith('/api/v1/auth') ||
      pathname.startsWith('/_next') || pathname.startsWith('/favicon')) {
    return NextResponse.next();
  }

  // 无 token → 跳 /login?redirect=当前路径
  if (!token) {
    const redirect = encodeURIComponent(pathname + req.nextUrl.search);
    return NextResponse.redirect(new URL(`/login?redirect=${redirect}`, req.url));
  }

  // 请求带上 Authorization（middleware 只能改 request headers，不能改后续 fetch）
  // 真正带 token 由 axios 拦截器做
  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|health).*)'],
};
```

> 决策：**同时用 cookie 存 token + localStorage**。cookie 用于 Next.js middleware SSR 守卫（它读不到 localStorage），localStorage 用于 axios 拦截器读 token。双写双读。

### 3.5 修改 `src/stores/userStore.ts` 扩展
login() 里除了写 localStorage，也要写 cookie：`document.cookie = "kp_token=xxx; path=/; max-age=604800; SameSite=Lax"`

### 3.6 新建 `src/app/login/page.tsx`
- Ant Design 组件：`Form` + `Input` + `Button` + `Typography`
- 字段：用户名/邮箱、密码
- 提交调 `useUserStore.login()` → 成功 redirect redirect 参数指向的页面（默认 /chat）
- 底部"还没账号？去注册 →"链接到 /register
- 视觉风格：参考现有 chat 页面——白色背景、圆角 8px、蓝色主色 #1677ff

### 3.7 新建 `src/app/register/page.tsx`
- Ant Design 组件
- 字段：邮箱（EmailStr 校验）、用户名（≥3字符）、密码（≥6字符）、确认密码（两次一致）
- 提交调 `useUserStore.register()` → 成功提示"注册成功，请登录"，redirect /login
- 底部"已有账号？去登录 →"链接

### 3.8 修改 `src/types/index.ts`
添加：
```ts
export interface UserOut {
  id: string;
  email: string;
  username: string;
  role: 'admin' | 'member';
}

export interface TokenResponse {
  access_token: string;
  token_type: 'bearer';
  expires_in: number;
  user: UserOut;
}
```

### 3.9 修改 `src/app/layout.tsx`
- 加 AuthProvider（从 cookie/localStorage 初始化 userStore）
- 或者：保持现有 Providers 不变，auth provider 可以做成独立的 Client Component 放 layout 里

### 3.10 修改 `src/app/page.tsx`
保留 `redirect('/chat')`（middleware 会自动保护）

---

## 四、用户信息注入 request.state.user

本次实现后端通过 `get_current_user` 依赖注入**自动**把 User ORM 挂到 request.state.user 上。因为 protected_router 已经在父级统一挂了 `dependencies=[Depends(get_current_user)]`，所以所有业务接口的 handler 内都可以 `request.state.user.id` / `request.state.user.role` 取当前用户。

实现点：`deps.py` 的 `get_current_user` 在返回前做 `request.state.user = user`。

---

## 五、不在本次范围（后续迭代）

- 旧 admin（hashed_password="not-set-yet"）的迁移：**init_db 重建时**会覆盖为正确哈希；或提供单独的 `reset-admin` 脚本
- 业务路由里的权限细化 TODO（knowledge_bases.py L38/54/66、documents.py L96、rag_service.py L332-338、chat.py L149）：**本次只通 infrastructure 层**，让 `request.state.user` 可用，业务代码可以后续逐步接
- 刷新 token / 双 token 机制（access + refresh）
- 角色权限检查 decorator（Admin 接口等）
- Token 黑名单（logout 时真正失效）

---

## 六、验证清单

### 后端
- [ ] pytest tests/test_auth.py -v → 全部通过
- [ ] pytest 全量回归 → 不引入既有测试失败
- [ ] curl POST /auth/register → 返回 201 + UserOut
- [ ] curl POST /auth/login → 返回 access_token
- [ ] curl GET /chat/conversations 无 token → 401
- [ ] curl GET /chat/conversations 带 Bearer token → 200
- [ ] curl GET /auth/me 带 token → 当前用户

### 前端
- [ ] npx tsc --noEmit → 无错误
- [ ] npx next lint → 无错误
- [ ] 浏览器未登录访问 /chat → 自动跳 /login
- [ ] 浏览器登录成功 → 跳回 /chat
- [ ] 浏览器刷新后保持登录（cookie + localStorage）
- [ ] 手动删 token → 访问任意业务接口 → 401 → 自动跳 /login
- [ ] axios 请求自动带 Authorization header（devtools network 可见）
- [ ] /register 页面功能完整 → 注册成功 → 跳 /login

---

## 七、决策与假设

| 决策 | 理由 |
| :--- | :--- |
| 用父级 APIRouter 统一挂 `dependencies=[Depends(get_current_user)]` | 避免每个 endpoint 忘加；experience #2280615 验证过 |
| JWT 必须**验签**（HS256 + SECRET_KEY） | experience #2280615 的反例：仅 base64 解码不验签可被伪造 |
| token 同时存 cookie + localStorage | middleware 读 cookie、axios 拦截器读 localStorage |
| /register 不自动登录 | 后端语义更清晰；前端注册页有显式跳转 |
| logout 只删前端 token | 无 session 表，生产后续加刷新 token 时再加服务端黑名单 |
| 默认 admin 固定密码 `admin123` | 方便演示；.env 可覆盖；生产必须修改 |
| 密码 ≥6 字符（后端校验） | 与后端 Pydantic 约束一致 |
