-- Record which chunking strategy produced an application's stored sections, so a
-- future strategy change is observable and can be re-extracted deliberately rather
-- than silently. Nullable: existing rows keep NULL until next extraction. This does
-- not touch embeddings, scores or the embedding index identity.
ALTER TABLE applications ADD COLUMN chunk_strategy TEXT;
INSERT INTO schema_version VALUES(3);
