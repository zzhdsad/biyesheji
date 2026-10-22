/**
 * 证据原文「中文翻译」能力（问题4/5：证据原文存在大量英文）。
 *
 * 链路约定：
 * - 判定在前端（needsTranslation）：拉丁字母占比 ≥ 30% 才提供翻译，
 *   少量英文术语的中文原文不做整段翻译（避免"翻译成奇怪的中文"）；
 * - 懒加载：用户展开某条英文证据时才请求（见 getEvidenceTranslation），
 *   不对历史数据批量翻译；
 * - 缓存：模块级 Map（key = 原文全文），同一会话内再次展开直接命中；
 * - 失败：返回固定 fallback 文案，不影响正常问答与证据展示；
 * - 翻译明确标注为 AI 生成（见 EvidencePanel），绝不冒充原始文献内容。
 */
import { translateEvidence } from '@/services/api';

/** 需要翻译的拉丁字母占比阈值（0.5 = 英文字母超过字母总数一半才提供翻译）。 */
export const TRANSLATION_LATIN_RATIO = 0.5;

/**
 * 统计拉丁字母 / CJK 汉字数量（其余字符不参与判定）。
 */
function countLetters(text: string): { latin: number; cjk: number } {
  let latin = 0;
  let cjk = 0;
  for (const ch of text) {
    if (/[a-zA-Z]/.test(ch)) latin += 1;
    else if (/[\u4e00-\u9fff]/.test(ch)) cjk += 1;
  }
  return { latin, cjk };
}

/**
 * 证据原文是否需要提供中文翻译（判定口径与后端 /chat/evidence/translate 一致）：
 * - 纯中文 / 中文为主（拉丁字母 ≤ 一半）：不需要（原文已为中文 / 少量英文术语）；
 * - 英文为主或中英混合且英文占比过半：需要；
 * - 无字母文本（空 / 纯符号数字）：不需要。
 */
export function needsTranslation(text: string | null | undefined): boolean {
  const { latin, cjk } = countLetters(text ?? '');
  const total = latin + cjk;
  if (total === 0 || latin === 0) return false;
  return latin / total >= TRANSLATION_LATIN_RATIO;
}

/** 翻译结果（translation 为后端译文；error 为统一 fallback 文案）。 */
export interface TranslationResult {
  translation: string | null;
  /** 原文已为中文：无需翻译（后端也不会调用 LLM）。 */
  isChinese: boolean;
  error?: string;
}

/** 翻译失败统一文案（用户可见）。 */
export const TRANSLATION_FALLBACK = '暂时无法生成中文翻译，请查看原文。';

/** 模块级缓存：原文全文 → 译文（同一会话内重复展开直接命中）。 */
const translationCache = new Map<string, string>();

/** 测试辅助：清空翻译缓存。 */
export function clearTranslationCache(): void {
  translationCache.clear();
}

/**
 * 获取证据原文的中文翻译（懒加载 + 缓存）。
 *
 * - 原文不需要翻译 → 直接返回 isChinese=true（不发请求）；
 * - 已缓存 → 直接返回（cached 语义）；
 * - 未缓存 → 调用后端 POST /chat/evidence/translate（后端另有自己的缓存），
 *   成功后写入本模块缓存；
 * - 失败（网络 / 后端 503）→ 返回 { error: TRANSLATION_FALLBACK }，
 *   不抛出、不影响证据面板其余内容。
 *
 * fetcher 参数供测试注入（生产默认走 services/api）。
 */
export async function getEvidenceTranslation(
  text: string,
  fetcher: (text: string) => Promise<{ translation: string | null; is_chinese: boolean }> = translateEvidence,
): Promise<TranslationResult> {
  if (!needsTranslation(text)) return { translation: null, isChinese: true };
  const cached = translationCache.get(text);
  if (cached != null) return { translation: cached, isChinese: false };
  try {
    const res = await fetcher(text);
    if (res.is_chinese) return { translation: null, isChinese: true };
    if (res.translation && res.translation.trim()) {
      translationCache.set(text, res.translation);
      return { translation: res.translation, isChinese: false };
    }
    return { translation: null, isChinese: false, error: TRANSLATION_FALLBACK };
  } catch {
    return { translation: null, isChinese: false, error: TRANSLATION_FALLBACK };
  }
}
