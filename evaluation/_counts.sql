CREATE OR REPLACE FUNCTION pg_temp.tbl_counts() RETURNS TABLE(tbl text, n bigint) AS $fn$
DECLARE
  r record;
  c bigint;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename LOOP
    EXECUTE format('SELECT count(*) FROM %I', r.tablename) INTO c;
    tbl := r.tablename;
    n := c;
    RETURN NEXT;
  END LOOP;
END $fn$ LANGUAGE plpgsql;

SELECT * FROM pg_temp.tbl_counts();
