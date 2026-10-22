/**
 * 中医文献来源可信度常量（与后端 src/core/source_meta.py 固定映射一致）。
 * 映射不得在前端自行改动：国家标准=5、规划教材=4、经典古籍=3、后世医家=2、民间偏方=1。
 */
import type { SourceEra, SourceType } from '@/types';

export const SOURCE_TYPE_OPTIONS: { label: SourceType; value: SourceType }[] = [
  { label: '国家标准', value: '国家标准' },
  { label: '规划教材', value: '规划教材' },
  { label: '经典古籍', value: '经典古籍' },
  { label: '后世医家', value: '后世医家' },
  { label: '民间偏方', value: '民间偏方' },
];

export const SOURCE_ERA_OPTIONS: { label: SourceEra; value: SourceEra }[] = [
  { label: '先秦', value: '先秦' },
  { label: '汉', value: '汉' },
  { label: '唐', value: '唐' },
  { label: '宋', value: '宋' },
  { label: '明', value: '明' },
  { label: '清', value: '清' },
  { label: '现代', value: '现代' },
];

/** 来源类型 → 可信度等级（后端唯一映射的前端镜像，仅用于只读展示）。 */
export const CREDIBILITY_MAP: Record<SourceType, number> = {
  国家标准: 5,
  规划教材: 4,
  经典古籍: 3,
  后世医家: 2,
  民间偏方: 1,
};

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
