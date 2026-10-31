-- 统计当前库所有表的精确行数（用于迁移前后对比，确认业务数据未受影响）
SELECT table_name || '|' ||
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I', table_name),
                           false, true, '')))[1]::text::int
FROM information_schema.tables
WHERE table_schema = 'public'
ORDER BY table_name;
