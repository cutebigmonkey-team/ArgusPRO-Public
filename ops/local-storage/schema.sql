CREATE SCHEMA IF NOT EXISTS argus_archive;
CREATE TABLE IF NOT EXISTS argus_archive.packages (
  package_id text PRIMARY KEY,
  manifest jsonb NOT NULL,
  imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS argus_archive.posts (
  canonical_key text PRIMARY KEY,
  platform text NOT NULL,
  post_url text NOT NULL,
  text_content text NOT NULL,
  published_at timestamptz,
  observed_at timestamptz NOT NULL,
  payload jsonb NOT NULL,
  last_package_id text NOT NULL REFERENCES argus_archive.packages(package_id)
);
CREATE TABLE IF NOT EXISTS argus_archive.media (
  sha256 text PRIMARY KEY,
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  relative_path text NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS argus_archive.post_media (
  canonical_key text NOT NULL REFERENCES argus_archive.posts(canonical_key),
  sha256 text NOT NULL REFERENCES argus_archive.media(sha256),
  PRIMARY KEY (canonical_key, sha256)
);
CREATE INDEX IF NOT EXISTS posts_observed_at ON argus_archive.posts(observed_at);
