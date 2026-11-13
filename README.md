# ARGUS PRO — isolated source candidate

This directory contains application source and synthetic fixtures, with no
imported Git ancestry and no configured remote. It is not a verified production
release. Do not deploy it until private-asset completeness and all release gates
are verified. No production resources were modified while preparing it.

## Runtime configuration

Keep archive credentials in existing Hostless secure bindings:
`ARGUS_DRIVE_OAUTH`, `ARGUS_DRIVE_FOLDER_ID`, `DATABASE_URL`, optional
`ARGUS_DATABASE_SESSION_URL`, `ARGUS_JSONBIN_KEY`, `ARGUS_JSONBIN_BIN_ID`,
`ARGUS_JSONBIN_KEY_TYPE`, `ARGUS_RUNTIME_MODE`, `ARGUS_ARCHIVE_WRITER_OWNER`.
AI uses existing `TYPESAFE_API_KEY` and private AI configuration.

Production source configuration is read from the existing private Drive
`sources.json`, or from `ARGUS_PRIVATE_SOURCE_FILE` outside this source tree.
Collection additionally requires an owner-confirmed positive
`ARGUS_MIN_PRIVATE_SOURCES`. Missing, empty, duplicate-ID or undersized registries
fail closed. Public `sources.json` is an empty fixture and never a production
fallback. Verify all source IDs and existing cursor ownership before any rollout.
Private registry approval/submission handling remains in the existing pipeline.

Optional private configuration can be mounted outside the source tree via
`ARGUS_PRIVATE_ASSETS_DIR`: reviewed origins/YouTube pins, discovery policy,
platform views, source reviews, MAX mappings, public-surface aliases and enhanced
per-source settings. Supply only reviewed existing values. Do not create a new
archive location or change folder permissions. Private importer compatibility
uses optional `ARGUS_LEGACY_SHARE_IDS_JSON` and `ARGUS_SOURCE_SHEET_URL`.

The television catalog defaults to an empty public schema; tests use synthetic
channels. A production programming catalog needs a separate approved runtime
binding before TV functionality can be considered compatible. No private catalog
is included in the Docker build context.

See `ops/security/hostless.public.example.env`. Templates contain empty values;
do not source them into a running worker. Public CI has no production secrets,
writer credentials, production databases, deployment steps or collector schedules.

## Validation

The public workflow prepares security, frontend, real-engine, Chromium,
PostgreSQL, collector/archive and exact Docker gates. It is not yet verified in a
supported builder. Known optional parser fixture requirements must be satisfied
without suppressing skips. Full third-party secret scanning, owner review of
licensing/publication rights and exact Docker validation remain required.
