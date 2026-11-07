/**
 * 结构化数据展示的公共常量与纯函数（第三 / 四 / 五阶段共用）。
 *
 * 这些定义原先在结构化预览抽屉与关联数据抽屉里各写了一份，内容完全相同；
 * 集中到这里后，字段类型中文、单元格取值与分页口径只有一处，不会再出现
 * 「一个页面改了另一个没改」的偏差。展示逻辑本身不变。
 */

/** 每次只取一页，绝不全量加载。 */
export const DEFAULT_PAGE_SIZE = 20;
export const PAGE_SIZE_OPTIONS = [20, 50, 100, 200];

/** 后端推断的字段类型 → 中文展示（未识别的类型原样显示，不做猜测）。 */
export const FIELD_TYPE_LABELS: Record<string, string> = {
  integer: '整数',
  float: '小数',
  bool: '布尔',
  date: '日期',
  percent: '百分比',
  string: '文本',
  mixed: '混合',
  null: '全空',
};

export function fieldTypeLabel(type: string | null | undefined): string {
  if (!type) return '';
  return FIELD_TYPE_LABELS[type] ?? type;
}

/**
 * 结构化解析的业务细粒度文案（structured_parses.status）。
 * 统一状态词汇见 `@/utils/processingStatus`；这里保留「记录已生成」这类
 * 更贴近业务的信息，用于在统一标签旁说明具体走到了哪一步。
 */
export const STRUCTURED_PARSE_STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pending: { label: '待解析', color: 'default' },
  parsing: { label: '解析中', color: 'processing' },
  success: { label: '记录已生成', color: 'success' },
  failed: { label: '解析失败', color: 'error' },
};

export function structuredParseStatusMeta(
  status: string | null | undefined,
): { label: string; color: string } {
  const key = status ?? '';
  return STRUCTURED_PARSE_STATUS_LABELS[key] ?? { label: key || '未知', color: 'default' };
}

/** 原始值展示：空值单独标注，嵌套结构原样序列化（不改写数据）。 */
export function cellValueText(raw: unknown): string {
  if (raw === null || raw === undefined) return '';
  if (typeof raw === 'object') return JSON.stringify(raw, null, 0) ?? '';
  return String(raw);
}
