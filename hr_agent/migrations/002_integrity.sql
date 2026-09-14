ALTER TABLE jobs ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roles ADD COLUMN jd_error TEXT;
CREATE TRIGGER rubric_frozen BEFORE UPDATE ON rubrics WHEN OLD.approved_at IS NOT NULL
BEGIN SELECT RAISE(ABORT,'Approved rubric versions are immutable'); END;
INSERT INTO schema_version VALUES(2);
