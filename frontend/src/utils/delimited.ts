/**
 * 表格编辑 → CSV/TSV 文本序列化。
 *
 * 保存后由后端重新解析入库，因此必须正确转义引号、换行与分隔符，
 * 否则含逗号/换行的单元格会破坏表格结构。
 */

export type TableDelimiter = ',' | '\t';

function quoteCell(cell: string, delimiter: TableDelimiter): string {
  if (
    cell.includes('"') ||
    cell.includes('\n') ||
    cell.includes('\r') ||
    cell.includes(delimiter)
  ) {
    return `"${cell.replace(/"/g, '""')}"`;
  }
  return cell;
}

export function serializeDelimited(
  rows: string[][],
  delimiter: TableDelimiter,
): string {
  return rows
    .map((row) => row.map((cell) => quoteCell(cell ?? '', delimiter)).join(delimiter))
    .join('\n');
}

/** 规范化为矩形表格（补齐缺失单元格），便于 AntD Table 稳定渲染。 */
export function normalizeTableRows(rows: string[][]): string[][] {
  const width = rows.reduce((max, row) => Math.max(max, row.length), 0);
  return rows.map((row) => [
    ...row.map((cell) => cell ?? ''),
    ...Array.from({ length: width - row.length }, () => ''),
  ]);
}
