SELECT 'documents' AS t, count(*) AS total,
       count(*) FILTER (WHERE import_batch_id IS NOT NULL) AS imported,
       count(*) FILTER (WHERE import_batch_id IS NULL) AS test
FROM documents
UNION ALL SELECT 'documents_alive', count(*) FILTER (WHERE deleted_at IS NULL), 0, 0 FROM documents
UNION ALL SELECT 'documents_soft_deleted', count(*) FILTER (WHERE deleted_at IS NOT NULL), 0, 0 FROM documents
UNION ALL SELECT 'documents_marked_vectorized', count(*) FILTER (WHERE vector_model IS NOT NULL), 0, 0 FROM documents
UNION ALL
SELECT 'chunks', count(*),
       count(*) FILTER (WHERE d.import_batch_id IS NOT NULL),
       count(*) FILTER (WHERE d.import_batch_id IS NULL OR d.id IS NULL)
FROM chunks c LEFT JOIN documents d ON d.id = c.doc_id
UNION ALL SELECT 'chunks_orphan_no_doc', count(*) FILTER (WHERE d.id IS NULL), 0, 0
FROM chunks c LEFT JOIN documents d ON d.id = c.doc_id
UNION ALL SELECT 'herbs', count(*),
       count(*) FILTER (WHERE import_batch_id IS NOT NULL),
       count(*) FILTER (WHERE import_batch_id IS NULL) FROM herbs
UNION ALL SELECT 'prescriptions', count(*),
       count(*) FILTER (WHERE import_batch_id IS NOT NULL),
       count(*) FILTER (WHERE import_batch_id IS NULL) FROM prescriptions
UNION ALL SELECT 'theories', count(*),
       count(*) FILTER (WHERE import_batch_id IS NOT NULL),
       count(*) FILTER (WHERE import_batch_id IS NULL) FROM theories
UNION ALL SELECT 'literatures', count(*),
       count(*) FILTER (WHERE import_batch_id IS NOT NULL),
       count(*) FILTER (WHERE import_batch_id IS NULL) FROM literatures
UNION ALL SELECT 'prescription_ingredients', count(*), 0, 0 FROM prescription_ingredients
UNION ALL SELECT 'knowledge_base_resources', count(*), 0, 0 FROM knowledge_base_resources
UNION ALL SELECT 'knowledge_bases', count(*), 0, 0 FROM knowledge_bases
UNION ALL SELECT 'kb_members', count(*), 0, 0 FROM kb_members
UNION ALL SELECT 'conversations', count(*), 0, 0 FROM conversations
UNION ALL SELECT 'messages', count(*), 0, 0 FROM messages
UNION ALL SELECT 'import_jobs', count(*), 0, 0 FROM import_jobs
UNION ALL SELECT 'revectorize_jobs', count(*), 0, 0 FROM revectorize_jobs
UNION ALL SELECT 'test_cases', count(*), 0, 0 FROM test_cases
UNION ALL SELECT 'evaluation_runs', count(*), 0, 0 FROM evaluation_runs
UNION ALL SELECT 'evaluation_results', count(*), 0, 0 FROM evaluation_results
UNION ALL SELECT 'feedbacks', count(*), 0, 0 FROM feedbacks
UNION ALL SELECT 'kg_nodes', count(*), 0, 0 FROM kg_nodes
UNION ALL SELECT 'kg_edges', count(*), 0, 0 FROM kg_edges
UNION ALL SELECT 'users', count(*), 0, 0 FROM users
UNION ALL SELECT 'audit_logs', count(*), 0, 0 FROM audit_logs
UNION ALL SELECT 'categories', count(*), 0, 0 FROM categories
UNION ALL SELECT 'tags', count(*), 0, 0 FROM tags
UNION ALL SELECT 'distinct_import_batches', count(DISTINCT import_batch_id), 0, 0 FROM documents
ORDER BY 1;
