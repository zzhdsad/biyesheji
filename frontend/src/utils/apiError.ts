/**
 * axios 错误 → 用户可读信息。
 *
 * 后端有两类错误体（本项目以第 1 类为主）：
 * 1. 业务异常 AppException（src/core/exceptions.py 全局处理器）：
 *    `{ code: 400, message: "LLM Base URL 不合法：..." }` —— 没有 detail 字段
 * 2. FastAPI 原生校验错误：`{ detail: "..." | [...] }`
 *
 * 只读 detail 会拿不到第 1 类的原因，退化成 axios 通用消息
 * "Request failed with status code 400"（BUG：设置页测试连通等场景）。
 */

interface AxiosLikeError {
  response?: { data?: { detail?: unknown; message?: unknown } };
  message?: string;
}

/** 从 axios 错误中提取后端返回的可读信息；都取不到时回退到通用提示。 */
export function describeApiError(err: unknown): string {
  const e = err as AxiosLikeError;
  const data = e?.response?.data;
  // 1) 后端 AppException 的统一错误体 { code, message }
  const bizMsg = data?.message;
  if (typeof bizMsg === 'string' && bizMsg) return bizMsg;
  // 2) FastAPI 原生错误体 { detail }（字符串时直接展示）
  const detail = data?.detail;
  if (typeof detail === 'string' && detail) return detail;
  // 3) 网络错误 / 无响应体：axios 自带 message（如 "Network Error"）
  if (e?.message) return e.message;
  return '请求失败，请检查后端服务是否正常';
}
