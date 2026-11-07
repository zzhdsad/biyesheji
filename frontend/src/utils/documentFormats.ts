/** 前端展示用的文档格式能力表（与后端 src/core/document_formats.py 保持一致）。 */

export interface FormatCapability {
  /** 扩展名（小写，不含点）。 */
  ext: string;
  /** 界面展示名。 */
  label: string;
  /** 是否支持在线编辑。 */
  editable: boolean;
  /** 编辑方式标签（不可编辑时为空串）。 */
  editLabel: string;
  /** 分类说明：预览方式与编辑能力。 */
  note: string;
}

/** 文档管理页「格式支持说明」展示的 10 种格式。 */
export const DOCUMENT_FORMATS: FormatCapability[] = [
  {
    ext: 'docx',
    label: 'DOCX',
    editable: false,
    editLabel: '',
    note: '提取标题与段落预览，不支持在线编辑',
  },
  {
    ext: 'pdf',
    label: 'PDF',
    editable: false,
    editLabel: '',
    note: '浏览器内渲染原始文件，不支持在线编辑',
  },
  {
    ext: 'md',
    label: 'MD',
    editable: true,
    editLabel: '文本编辑',
    note: '文本编辑，按标题层级切片',
  },
  {
    ext: 'txt',
    label: 'TXT',
    editable: true,
    editLabel: '文本编辑',
    note: '文本编辑',
  },
  {
    ext: 'csv',
    label: 'CSV',
    editable: true,
    editLabel: '表格编辑',
    note: '表格编辑，切片按行组织并保留表头',
  },
  {
    ext: 'tsv',
    label: 'TSV',
    editable: true,
    editLabel: '表格编辑',
    note: '表格编辑，切片按行组织并保留表头',
  },
  {
    ext: 'xlsx',
    label: 'XLSX',
    editable: false,
    editLabel: '',
    note: '按工作表预览，不支持在线编辑',
  },
  {
    ext: 'json',
    label: 'JSON',
    editable: true,
    editLabel: '源码编辑',
    note: '结构树预览 + 源码编辑，保存前校验 JSON 合法性',
  },
  {
    ext: 'html',
    label: 'HTML',
    editable: true,
    editLabel: '源码编辑',
    note: '沙箱渲染（已清洗，不执行脚本）+ 源码编辑',
  },
  {
    ext: 'pptx',
    label: 'PPTX',
    editable: false,
    editLabel: '',
    note: '按幻灯片页预览，不支持在线编辑',
  },
];

/** 支持在线编辑的格式（由上表派生，避免两处漂移）。 */
export const EDITABLE_FORMATS: ReadonlySet<string> = new Set(
  DOCUMENT_FORMATS.filter((f) => f.editable).map((f) => f.ext),
);

/** 图片格式（仅上传/预览/下载，不在格式说明表中列出）。 */
export const IMAGE_FORMATS: ReadonlySet<string> = new Set([
  'png',
  'jpg',
  'jpeg',
  'gif',
  'webp',
  'bmp',
]);

/**
 * 上传与替换文件选择器使用的 accept 串。
 *
 * 10 种文档格式 + 图片格式；替换场景按当前文档扩展名单独限制，见
 * `acceptFor(fileType)`。
 */
export const UPLOAD_ACCEPT = [
  ...DOCUMENT_FORMATS.map((f) => f.label.toLowerCase()),
  ...Array.from(IMAGE_FORMATS),
]
  .map((e) => `.${e}`)
  .join(',');

/** 上传提示文案（与 UPLOAD_ACCEPT 同源，避免两处漂移）。 */
export const UPLOAD_HINT = DOCUMENT_FORMATS.map((f) => f.label).join(' / ');

/** 允许上传的扩展名（10 种文档格式 + 图片），与 UPLOAD_ACCEPT 同源。 */
export const UPLOADABLE_FORMATS: ReadonlySet<string> = new Set([
  ...DOCUMENT_FORMATS.map((f) => f.ext),
  ...Array.from(IMAGE_FORMATS),
]);

/**
 * 该扩展名是否可上传。
 *
 * 「上传文件夹」会一次带入整个目录树（含 .DS_Store、临时文件等），
 * 用它在本地先筛掉后端必定拒绝的文件，避免无意义的请求。
 */
export function isUploadableFormat(fileType: string): boolean {
  return UPLOADABLE_FORMATS.has(fileType.toLowerCase());
}

/** 结构化解析支持的格式（第三阶段：CSV / TSV / XLSX / JSON）。 */
export const STRUCTURED_FORMATS: readonly string[] = ['csv', 'tsv', 'xlsx', 'json'];

/** 该扩展名是否会生成结构化知识记录。 */
export function isStructuredFormat(fileType: string): boolean {
  return STRUCTURED_FORMATS.includes(fileType.toLowerCase());
}

/** 限制到指定格式的文件选择器 accept 串。 */
export function acceptFor(fileType: string): string {
  return `.${fileType.toLowerCase()}`;
}

/** 该格式是否支持在线编辑（决定操作栏与预览抽屉是否显示「编辑」）。 */
export function isEditableFormat(fileType: string): boolean {
  return EDITABLE_FORMATS.has(fileType.toLowerCase());
}
