/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
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
