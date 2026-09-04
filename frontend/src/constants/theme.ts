/** 主题相关常量（服务端/客户端共用，不得放入 'use client' 模块）。
 *
 * 注意：layout.tsx 是服务端组件，若从 'use client' 模块导入值，
 * 会被替换成 client reference 对象（内插成 '[object Object]'）。
 */
export const THEME_STORAGE_KEY = 'kp-theme';
