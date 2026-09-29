-- Canvas: links from an artifact to tickets and entities (ticket T-59).
-- Polymorphic like note_links: target_id has no FK, so integrity is kept by
-- the API. Entity delete is refused while linked (ENTITY_REFERENCES), and
-- ticket delete removes the ticket's links but never the artifact.
-- Created after 0015's rebuild of artifacts, so the FK names the new table.
CREATE TABLE artifact_links (
    id          TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    target_type TEXT NOT NULL CHECK (target_type IN ('ticket', 'entity')),
    target_id   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (artifact_id, target_type, target_id)
);
CREATE INDEX idx_artifact_links_target ON artifact_links(target_type, target_id);
