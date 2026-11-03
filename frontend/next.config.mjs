/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  experimental: {
    // Next 的 rewrite 代理默认 30s 超时：大文件导入/向量化会被直接切断，
    // 前端只看到 500。放宽到 10 分钟，与 api.ts 中长任务的 axios 超时一致。
    proxyTimeout: 600000,
  },
  async rewrites() {
    // 开发代理：/api/* 转发到 FastAPI，避免跨域
    const backend = process.env.BACKEND_URL || 'http://127.0.0.1:8000';
    return [
      {
        source: '/api/:path*',
        destination: `${backend}/api/:path*`,
      },
      {
        source: '/health',
        destination: `${backend}/health`,
      },
    ];
  },
};

export default nextConfig;
