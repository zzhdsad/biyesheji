/**
 * 来源可信度展示常量（与后端 src/core/source_meta.py 固定映射一致）。
 * 可信度等级由后端按来源类型推导，前端仅负责只读展示配色。
 */

/** 可信度等级对应的 Tag 颜色（5 高 → 1 低）。 */
export const CREDIBILITY_COLOR: Record<number, string> = {
  5: 'success',
  4: 'blue',
  3: 'gold',
  2: 'orange',
  1: 'red',
};

/** 来源类型 Tag 颜色（与可信度等级同色，便于一眼识别）。 */
export function credibilityColor(level?: number | null): string {
  if (!level) return 'default';
  return CREDIBILITY_COLOR[level] ?? 'default';
}
