/**
 * 映射模板的前端纯函数工具：字段差异、配置构建与可用性判定。
 *
 * 后端 `mapping_template_service._field_diff` 只校验模板**真正依赖**的字段
 * （主键 + 关系/关联字段），并允许数据集新增列。前端展示与提交前必须使用同一
 * 口径，否则会出现"界面显示可用、后端判定缺字段"的分歧，甚至静默套用错误映射。
 */
import type {
  ImportTemplateConfig,
  ImportTemplateMatchStatus,
} from '@/services/api';

interface TableLike {
  table: string;
  fields: { name: string }[];
}

interface ForeignKeyLike {
  child_table: string;
  child_field: string;
  parent_table: string;
  parent_field: string;
  relation_type?: string;
}

interface JunctionLike {
  table: string;
  left_table: string;
  left_field: string;
  right_table: string;
  right_field: string;
  relation_type?: string;
}

export interface TemplateFieldDiff {
  missing_fields: Record<string, string[]>;
  extra_fields: Record<string, string[]>;
  status: Extract<ImportTemplateMatchStatus, 'exact' | 'compatible' | 'mismatch'>;
  usable: boolean;
}

/** 由分析结果构造 `{表名: [字段名]}`（模板匹配/保存的统一入参）。 */
export function tablesFieldMap(tables: TableLike[]): Record<string, string[]> {
  const map: Record<string, string[]> = {};
  for (const table of tables) {
    map[table.table] = table.fields.map((field) => field.name);
  }
  return map;
}

function normalize(current: Record<string, string[]>): Record<string, Set<string>> {
  const out: Record<string, Set<string>> = {};
  for (const [table, fields] of Object.entries(current)) {
    out[table] = new Set(fields.filter(Boolean).map((field) => field.trim().toLowerCase()));
  }
  return out;
}

/**
 * 比对模板依赖字段与当前数据集，口径与后端 `_field_diff` 保持一致：
 * 缺字段/缺整表 → mismatch；仅新增未被模板覆盖的表 → compatible。
 */
export function diffTemplate(
  config: ImportTemplateConfig,
  currentTables: Record<string, string[]>,
): TemplateFieldDiff {
  const current = normalize(currentTables);
  const missing: Record<string, string[]> = {};
  const covered = new Set<string>();

  const need = (table: string, field: string | null | undefined) => {
    if (!field) return;
    const fields = current[table];
    if (!fields) {
      const list = missing[table] ?? (missing[table] = []);
      if (!list.includes('<整张表缺失>')) list.push('<整张表缺失>');
      return;
    }
    if (!fields.has(field.trim().toLowerCase())) {
      (missing[table] ?? (missing[table] = [])).push(field);
    }
  };

  for (const [table, mapping] of Object.entries(config.tables ?? {})) {
    covered.add(table);
    need(table, mapping.pk_field);
  }
  for (const relation of config.relations ?? []) {
    covered.add(relation.child_table);
    covered.add(relation.parent_table);
    need(relation.child_table, relation.child_field);
    need(relation.parent_table, relation.parent_field);
  }
  for (const junction of config.junctions ?? []) {
    covered.add(junction.table);
    need(junction.table, junction.left_field);
    need(junction.table, junction.right_field);
  }

  const extra: Record<string, string[]> = {};
  for (const [table, fields] of Object.entries(currentTables)) {
    if (!covered.has(table)) {
      extra[table] = [...fields].sort();
    }
  }

  const hasMissing = Object.keys(missing).length > 0;
  return {
    missing_fields: missing,
    extra_fields: extra,
    status: hasMissing ? 'mismatch' : Object.keys(extra).length > 0 ? 'compatible' : 'exact',
    usable: !hasMissing,
  };
}

/** 只有后端判定 exact/compatible 且前端复核无缺失时，才允许直接套用模板。 */
export function canApplyTemplate(
  matchStatus: ImportTemplateMatchStatus,
  diff: TemplateFieldDiff,
): boolean {
  return (matchStatus === 'exact' || matchStatus === 'compatible') && diff.usable;
}

/** 把页面上的映射选择固化为可保存的模板配置（version 2）。 */
export function buildTemplateConfig(params: {
  tables: string[];
  primaryKeys: Record<string, string>;
  foreignKeys: ForeignKeyLike[];
  junctions: JunctionLike[];
}): ImportTemplateConfig {
  const tableMap: ImportTemplateConfig['tables'] = {};
  for (const table of params.tables) {
    tableMap[table] = {
      table,
      entity_type: table,
      pk_field: params.primaryKeys[table] || null,
      display_field: null,
    };
  }
  return {
    version: 2,
    tables: tableMap,
    relations: params.foreignKeys.map((item) => ({
      child_table: item.child_table,
      child_field: item.child_field,
      parent_table: item.parent_table,
      parent_field: item.parent_field,
      relation_type: item.relation_type || 'related_to',
    })),
    junctions: params.junctions.map((item) => ({
      table: item.table,
      left_table: item.left_table,
      left_field: item.left_field,
      right_table: item.right_table,
      right_field: item.right_field,
      relation_type: item.relation_type || 'related_to',
    })),
  };
}

/** 模板中声明的主键（表名 → 字段），用于套用时回填主键选择。 */
export function templatePrimaryKeys(config: ImportTemplateConfig): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [table, mapping] of Object.entries(config.tables ?? {})) {
    if (mapping.pk_field) out[table] = mapping.pk_field;
  }
  return out;
}

/** 模板依赖的字段清单（用于详情展示：让用户看到模板到底依赖什么）。 */
export function templateDependencyFields(
  config: ImportTemplateConfig,
): { table: string; field: string; kind: string }[] {
  const rows: { table: string; field: string; kind: string }[] = [];
  for (const [table, mapping] of Object.entries(config.tables ?? {})) {
    if (mapping.pk_field) rows.push({ table, field: mapping.pk_field, kind: '主键' });
  }
  for (const relation of config.relations ?? []) {
    rows.push({ table: relation.child_table, field: relation.child_field, kind: '外键' });
    rows.push({ table: relation.parent_table, field: relation.parent_field, kind: '被引用键' });
  }
  for (const junction of config.junctions ?? []) {
    rows.push({ table: junction.table, field: junction.left_field, kind: '关联键' });
    rows.push({ table: junction.table, field: junction.right_field, kind: '关联键' });
  }
  return rows;
}
