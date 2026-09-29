import { test } from 'node:test';
import assert from 'node:assert/strict';

import { describeApiError } from './apiError';

test('后端 AppException 错误体 { code, message }：返回 message 而非 axios 通用消息', () => {
  // 复现设置页「测试连通」→ POST /settings/model/test 的真实 400 响应体
  // （SSRF 校验：LLM Base URL 为 localhost 被拒绝）
  const err = {
    response: {
      status: 400,
      data: {
        code: 400,
        message: 'LLM Base URL 不合法：仅允许可公网访问的 http/https 地址，禁止内网、本机、回环与云元数据地址',
      },
    },
    message: 'Request failed with status code 400',
  };
  assert.match(describeApiError(err), /LLM Base URL 不合法/);
  // 回归：不得再显示 axios 的通用错误文本
  assert.notEqual(describeApiError(err), 'Request failed with status code 400');
});

test('Embedding 重新向量化相关的 AppException 400 同样可读（发起/重试/取消）', () => {
  const cases = [
    '没有需要重新向量化的文档（存量向量均已是当前模型）',
    '该任务没有失败项，无需重试',
    '任务已结束（succeeded），无需取消',
  ];
  for (const message of cases) {
    const err = {
      response: { status: 400, data: { code: 400, message } },
      message: 'Request failed with status code 400',
    };
    assert.equal(describeApiError(err), message);
  }
});

test('FastAPI 原生 detail（字符串）仍然优先于 axios message', () => {
  const err = {
    response: { status: 400, data: { detail: '字段不合法' } },
    message: 'Request failed with status code 400',
  };
  assert.equal(describeApiError(err), '字段不合法');
});

test('AppException message 优先于 FastAPI detail（本项目错误体以前者为主）', () => {
  const err = {
    response: { status: 400, data: { code: 400, message: '业务原因', detail: 'not this' } },
    message: 'Request failed with status code 400',
  };
  assert.equal(describeApiError(err), '业务原因');
});

test('无响应体（网络错误）回退到 axios message', () => {
  assert.equal(describeApiError({ message: 'Network Error' }), 'Network Error');
});

test('完全无法识别的错误对象回退到通用提示', () => {
  assert.equal(describeApiError(null), '请求失败，请检查后端服务是否正常');
  assert.equal(describeApiError({}), '请求失败，请检查后端服务是否正常');
});
