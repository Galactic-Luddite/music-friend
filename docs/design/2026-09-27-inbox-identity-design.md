# Inbox identity: one inbox item per release, by construction

Status: draft for review. Supersedes the dedupe patches in #47, #53 and #55 as the model of
record; absorbs #56 and #57. Companion to
[2026-09-24-release-source-design.md](2026-09-24-release-source-design.md).

All titles, artists and identifiers in this document are synthetic.

## 1. Problem

Duplicate inbox items keep appearing, and every fix so far has patched one duplicate pattern
after it was observed on a real library:

| Pattern | Seen | Patched by |
|---|---|---|
| Same-source repeat: one release surfaced again on a later run | #50 | #53 (content-only `material_version` plus a v1 replay) |
| Cross-source title variants: `(Remix)` vs `(X Remix)`, `(from Some Film: The Album)` vs plain, partial artist credits | #50, #54 | #53, #55 (title-key folding) |
| Curly vs straight apostrophe across sources | #56 | open |
| Repair re-created 12 signals and inbox items after Deezer attached a second source reference to 12 existing releases; inbox 24 -> 36 | #57 | open |

The patches share a root cause. An inbox item today has no identity of its own: `inbox_entries`
is keyed one-to-one to a `signals` row
(`signal_local_id TEXT NOT NULL UNIQUE`, `003_application_state.sql:141`), and a signal is
keyed by `(provider, kind, provider_native_id, material_version)`
(`003_application_state.sql:419`). `material_version` hashes the release's *provenance*
(`source_refs`, `refresh.py:1381 _release_material`) and the explanation's *reasons*, so any of
these mints a new signal and therefore a new inbox item:

- a second source attaching to the release (#57: `source_refs` changed, so
  `_release_repair_already_recorded` found no matching version under v2 or v1 and wrote a new
  signal);
- a change of reason between `new_release` and `updated_release` (#50);
- a discovery-side identity miss that creates a second `releases` row for the same real-world
  release (#54, #56), because cross-source identity is decided only by a fuzzy title key.

Nothing in the schema forbids two inbox entries for one release; the invariant lives in code,
spread across `_record_candidate`, `_repair_missing_signals`,
`_release_repair_already_recorded`, `_legacy_v1_material_version`,
`find_release_discovery_variant` and `_persist_artist_releases`, and every new write path (a
new source, a new repair) has to re-derive it.

Two adjacent problems were observed in the same runs and are in scope because they share the
same seams: every MCP tool blocks while a refresh runs (the refresh executes synchronously
inside the `refresh_music` handler on the server's only catalog connection,
`mcp_stdio.py:178-262`, `catalog_server.py:583-591`), and the `release_source_unmapped`
metric's meaning in a multi-source run is undefined.

**Goal:** A release that the catalog already knows never produces a second inbox item, whatever
path (discovery, repair, a new source, an upgrade) observes it again, and the store enforces
that rather than code.

## 2. Approach

Make identity explicit at three layers and let the database hold the invariant:

1. **Inbox identity = canonical record.** `inbox_entries` gains `(kind, record_local_id)` with a
   `UNIQUE` constraint. An inbox item is "the user's standing decision about this release (or
   event)". Signals become the item's *history* (why it appeared, what changed, from which
   source); an item points at its latest signal. No code path can create a second item for a
   record because the insert fails; the write path uses `ON CONFLICT DO UPDATE` to treat a
   re-observation as an update.
2. **Release identity = a resolution ladder, strong keys first.** Native ids already attached to
   a release (`source_references` is `UNIQUE (source, native_id)`), then cross-source links
   harvested from MusicBrainz release url-relationships (verified live: release-level
   `free streaming` / `streaming` relations point at Spotify and Deezer album URLs), then, as a
   later tier, barcodes, and only last the existing conservative title key. Every tier merges
   only when it identifies exactly one local release; anything else stays separate.
3. **One write path.** `record_release_observation` is the only function that turns an observed
   release into store state (release, provenance, discovery, signal, inbox). Discovery and repair
   both call it, so repair is a re-observation of stored state and is idempotent by
   construction: the second call returns `unchanged`.

Provenance is not content. The content digest that decides "did this release change" hashes
title, type, date, precision and credited artists; never `source_refs`, never the explanation.
Attaching a source is recorded as provenance (a `source_references` row plus an observation),
not as a change the user needs to see.

Existing duplicates are collapsed by a migration under the new constraint (most-decided state
wins), with the losing rows preserved in an audit table so the collapse is reversible, and a
CLI dry-run/merge operation handles the one class the migration must not touch: two distinct
`releases` rows that are the same real-world release.

Refresh runs on a worker thread with its own SQLite connection; MCP reads keep the server's
connection and never wait on the refresh (WAL readers do not block on a writer).

## 3. Components

### 3.1 Schema, migration `014_inbox_identity.sql`

`src/music_friend/store/schema/014_inbox_identity.sql`, registered in `migrations.py`.

```sql
-- 1. Audit table first, so the collapse below is reversible.
CREATE TABLE inbox_entry_merges (
    loser_local_id TEXT PRIMARY KEY,
    winner_local_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    record_local_id TEXT NOT NULL,
    loser_signal_local_id TEXT NOT NULL,
    loser_state TEXT NOT NULL,
    loser_created_at TEXT NOT NULL,
    loser_updated_at TEXT NOT NULL,
    merged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. Rebuild inbox_entries with record identity (SQLite cannot ADD a UNIQUE constraint).
CREATE TABLE inbox_entries_v2 (
    local_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('release', 'event')),
    record_local_id TEXT NOT NULL,
    latest_signal_local_id TEXT NOT NULL REFERENCES signals(local_id),
    state TEXT NOT NULL CHECK (state IN ('unread', 'saved', 'dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, record_local_id)
);

-- 3. Collapse: one winner per (kind, record). Rank: decided beats unread; among decided,
--    the most recently updated wins; ties break on local_id for determinism.
WITH ranked AS (
    SELECT e.local_id, s.kind, s.record_local_id, e.signal_local_id, e.state,
           e.created_at, e.updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY s.kind, s.record_local_id
               ORDER BY CASE e.state WHEN 'unread' THEN 1 ELSE 0 END,
                        e.updated_at DESC, e.local_id
           ) AS rank,
           MIN(e.created_at) OVER (PARTITION BY s.kind, s.record_local_id) AS first_created,
           MAX(e.updated_at) OVER (PARTITION BY s.kind, s.record_local_id) AS last_updated
    FROM inbox_entries AS e JOIN signals AS s ON s.local_id = e.signal_local_id
)
INSERT INTO inbox_entries_v2
SELECT local_id, kind, record_local_id, signal_local_id, state, first_created, last_updated
FROM ranked WHERE rank = 1;

WITH ranked AS ( /* same */ )
INSERT INTO inbox_entry_merges
    (loser_local_id, winner_local_id, kind, record_local_id, loser_signal_local_id,
     loser_state, loser_created_at, loser_updated_at)
SELECT l.local_id, w.local_id, l.kind, l.record_local_id, l.signal_local_id, l.state,
       l.created_at, l.updated_at
FROM ranked AS l JOIN ranked AS w
  ON w.kind = l.kind AND w.record_local_id = l.record_local_id AND w.rank = 1
WHERE l.rank > 1;

DROP TABLE inbox_entries;
ALTER TABLE inbox_entries_v2 RENAME TO inbox_entries;
CREATE INDEX inbox_entries_state_order ON inbox_entries (state, updated_at DESC, local_id);
CREATE INDEX inbox_entries_record ON inbox_entries (kind, record_local_id);
```

Notes:

- Window functions need SQLite 3.25 (2018). Python 3.10's oldest bundled SQLite on the
  supported platforms is newer; the migration test asserts `sqlite3.sqlite_version_info >=
  (3, 25, 0)` and `Catalog.open` raises `CatalogUnavailableError` with a clear message below it.
- Signals are not deleted or rewritten. A loser's signal stays in `signals` and is still
  reachable from `explain_inbox_item` as history (section 3.5).
- Inbox `local_id`s of losers stop resolving (`not_found`). These ids are conversational
  handles returned by `list_inbox`, not durable references; the doc for `update_inbox_item`
  already says to take the id from a fresh listing.
- `signals` keeps its `UNIQUE (provider, kind, provider_native_id, material_version)`; the
  meaning of `material_version` changes to a content digest (section 3.3). Existing rows are
  not rewritten; the write path never needs to reproduce an old digest again because it checks
  the inbox by record, not the signal by digest.
- `release_discoveries` (PK `release_local_id`, one source per release) stays for cursors and
  `first_seen_at`/`last_seen_at`; it stops being an identity lookup. Its
  `UNIQUE (source, provider_native_id)` remains and is no longer consulted for identity.
- Reversal: `music-friend data inbox unmerge --yes` re-inserts every `inbox_entry_merges` row
  into `inbox_entries` only when the constraint allows it, which it does not while the winner
  exists, so unmerge is defined as "restore the loser's *state* onto the winner when the loser
  was the more recent decision, and print what changed". Full reversal of the schema is by
  `data restore` of the backup taken before upgrading; the CHANGELOG entry and
  `docs/operations.md` say to run `music-friend data backup` before installing this version.
  `data restore` of a pre-014 backup into a 014 catalog goes through the portable importer
  (`portable.py`), which re-derives `(kind, record_local_id)` from each imported signal and
  applies the same collapse rule, so restore never violates the constraint.

### 3.2 Domain

`src/music_friend/domain/models.py`:

- `InboxEntry` gains `kind: SignalKind`, `record_local_id: RecordId`; `signal_local_id` is
  renamed `latest_signal_local_id` (portable export version bumps; importer accepts both).
- New frozen dataclasses in `domain/observations.py`:
  - `ReleaseObservation(release: Release, source: str, native_id: str, monitored_artist_local_id,
    external_links: tuple[SourceReference, ...], barcodes: tuple[str, ...], observed_at)`;
    `external_links` are the cross-source native ids the source itself asserts (MusicBrainz
    url-rels), each with `IdentityConfidence.EXTERNAL_ID`.
  - `ObservationOutcome(kind: Literal["created", "updated", "unchanged", "provenance_attached",
    "ambiguous"], release_local_id, inbox_local_id | None, method: IdentityMethod | None)`.
  - `IdentityMethod` enum: `native_id`, `external_link`, `barcode`, `title_key`, `none`.

### 3.3 Single write path

`src/music_friend/tools/release_observation.py` (application layer; imports store and domain,
never providers or MCP, per `tests/architecture/test_dependencies.py`).

```
record_release_observation(catalog, observation) -> ObservationOutcome
  with catalog.transaction():                       # one BEGIN IMMEDIATE, short
    match = resolve_release_identity(catalog, observation)      # section 3.4
    if match.ambiguous: return ambiguous (no writes except an observation row)
    if match.release_local_id is None:
        catalog.put_release(observation.release)   # new canonical release
        release_local_id = observation.release.local_id
    else:
        release_local_id = match.release_local_id
        catalog.attach_source_references(release_local_id,
            (observation source ref,) + observation.external_links)   # idempotent
        catalog.merge_release_content(release_local_id, observation.release)  # section 3.3.1
    catalog.put_release_identifiers(release_local_id, barcodes)      # tier 3 store
    catalog.touch_release_discovery(release_local_id, source, native_id, observed_at)
    content_version = _content_version(catalog.get_release(release_local_id))
    signal = catalog.find_signal_for_record(RELEASE, release_local_id, content_version)
    inbox = catalog.get_inbox_entry_for_record(RELEASE, release_local_id)
    if signal is None:
        reason = NEW_RELEASE if inbox is None else UPDATED_RELEASE
        signal = catalog.put_signal(Signal(..., material_version=content_version, reason))
    if inbox is None:
        catalog.upsert_inbox_entry(kind, release_local_id, signal.local_id, UNREAD)  # created
    elif inbox.latest_signal_local_id != signal.local_id:
        catalog.upsert_inbox_entry(kind, release_local_id, signal.local_id, inbox.state)  # updated
    else: unchanged / provenance_attached
```

`_content_version(release)` hashes `{"version": 3, "title", "release_type", "release_date",
"date_precision", "artist_refs"}` only. `source_refs`, `canonical_url`, `observed_at` and the
explanation are excluded by construction. `refresh.py`'s `_material_version`,
`_legacy_v1_material_version`, `_release_material`, `_release_repair_already_recorded` and the
release half of `_repair_missing_signals` are deleted, together with
`Catalog.list_release_discoveries_without_current_signal`.

`Catalog.upsert_inbox_entry` is the only inbox insert in the codebase:

```sql
INSERT INTO inbox_entries (local_id, kind, record_local_id, latest_signal_local_id, state,
                           created_at, updated_at)
VALUES (?, ?, ?, ?, 'unread', ?, ?)
ON CONFLICT (kind, record_local_id) DO UPDATE SET
    latest_signal_local_id = excluded.latest_signal_local_id,
    updated_at = excluded.updated_at
    -- state is deliberately NOT in the update list: a saved item stays saved and a
    -- dismissed item stays dismissed when its release changes.
```

`update_inbox_state` (user decisions) is the only writer of `state`.

#### 3.3.1 What an update means

| Observation | Store effect | User sees |
|---|---|---|
| Same content, same source, later run | `last_seen_at` advances | nothing (`unchanged`) |
| Second source attaches (the #57 case) | `source_references` + `record_sources` rows; an `observations` row `fact_name = 'source_attached'` | nothing (`provenance_attached`); `explain_inbox_item` lists both sources |
| Content changes (date correction, title fix) | new signal with `updated_release`; inbox `latest_signal_local_id`, `updated_at` move; `state` unchanged | the same item, with `updated_at` newer and `reasons` showing `updated_release`; not a new item, not resurrected from `dismissed` |
| Genuinely different release (deluxe, other remixer) | new release, new item | a new item |

`merge_release_content` decides what wins when two sources disagree on content for one
release: the source listed first in `release_sources` wins for `title`; the *more precise*
date wins for `release_date`/`date_precision` (DAY over MONTH over YEAR), otherwise first
source; `artist_refs` is the union in stable order. Whether a content change should re-surface
a dismissed item as unread is a product decision and is out of scope (section 6); the design
keeps `state` untouched.

### 3.4 Identity resolution ladder

`resolve_release_identity(catalog, observation) -> IdentityMatch(release_local_id | None,
method, ambiguous: bool)` in `tools/release_observation.py`, with the SQL lookups in
`store/catalog.py`.

| Tier | Key | Lookup | Merge when | Verified availability |
|---|---|---|---|---|
| 1 | `(source, native_id)` of the observation | `record_sources JOIN source_references` (`UNIQUE (source, native_id)`) | exactly one release row | Structural; already stored for every release |
| 2 | External links asserted by the source | each `(source, native_id)` in `observation.external_links` looked up as tier 1 | all links resolve to the same one release | MusicBrainz `GET /ws/2/release?release-group=<rgid>&inc=url-rels` returns per-release `free streaming`/`streaming` relations to `open.spotify.com/album/<id>` and `www.deezer.com/album/<id>`; release-group-level url-rels carry none (live probe 2026-09-26) |
| 3 (later) | Barcode (UPC/EAN) | `release_identifiers (scheme, value)` | exactly one release; digits-only, leading zeros stripped, length 11-14 | MusicBrainz release `barcode` in the same request as tier 2; Deezer `GET /album/<id>` has `upc` but `GET /artist/<id>/albums` does not (live probe 2026-09-26): one extra Deezer request per new album |
| 4 | Conservative title key | existing `find_release_discovery_variant`, generalized to `find_release_by_title_key` over `releases` + `release_artists` | exactly one release; artist-set overlap; same date (or +-1 day when both DAY) | Existing (#42, #50, #54); gains the #56 punctuation folding |

Rules that hold at every tier:

- **Exactly one or nothing.** A key that resolves to two or more distinct local releases is
  `ambiguous`: no merge, no new release, no inbox item; an `observations` row
  `fact_name = 'identity_ambiguous'` and a `release_identity_ambiguous` run metric. A false
  merge hides a real release, which is worse than a duplicate.
- **Same-source conflict guard.** A candidate never merges into a release that already carries a
  *different* native id for the candidate's own source (two Deezer album ids are two editions;
  deluxe vs standard is the typical case), regardless of what a lower tier says.
- **Tiers do not vote.** The first tier that returns exactly one release decides; lower tiers
  are not consulted. A lower-tier match that contradicts a higher-tier non-match is not a
  merge.
- **Title-key folding (#56).** `_normalized_title` and `_release_title_variant_key` share one
  `fold_title_key()` in `store/catalog.py`: NFKC, U+2018/2019/201B/2032 -> `'`,
  U+201C/201D/2033 -> `"`, U+2010..U+2015 and U+2212 -> `-`, U+2026 -> `...`, casefold,
  whitespace collapse. Display titles are never altered.

Order of arrival:

- *MusicBrainz first, Deezer later* (the common case): the MusicBrainz observation carries the
  Deezer album id as an external link, so the release already has a `deezer` source reference
  when Deezer reports it; Deezer hits tier 1.
- *Deezer first, MusicBrainz later* (the timeliness case Deezer exists for): Deezer creates the
  release. When MusicBrainz's release group appears with a url-rel to that Deezer album, tier 2
  merges into the Deezer-created release. If editors have not linked it yet, tier 4 applies; if
  tier 4 misses, two releases exist until a later harvest links them, at which point tier 2
  would point at a *different* existing release than tier 1: this is reported as
  `identity_conflict` (observation + metric), never auto-merged, and surfaces in
  `data inbox duplicates` (section 3.6).

Harvest cost: one MusicBrainz request per *new* release group (paced at 1/s inside the
existing `_PacedSource`), bounded per run by the existing deadline; a release group whose
harvest did not run this run is harvested on the next one (`release_link_harvest_pending`
cursor in `release_discoveries`). Tier 3 is designed here and deferred (section 6) until the
`identity_conflict`/`ambiguous` metrics show tier 2 plus tier 4 leave a measurable gap.

### 3.5 MCP and application surface (provider-neutral)

- `list_inbox` items: unchanged fields plus `updated_at` already present; `summary` unchanged.
  Nothing provider-specific is added.
- `explain_inbox_item`: `reasons` stays (latest signal); adds `history`: the record's signals
  oldest-first as `{observed_at, reasons}` and `sources`: count of source references (no
  provider ids, per the boundary in `tests/mcp/test_catalog_server.py`).
- `music_status`: `refresh.running: bool` (from the lock file lease) so an agent can avoid
  starting a second refresh and can explain a `partial` listing.
- `_TOOL_SCHEMAS`, `docs/mcp.md`, the skill and `tests/docs/test_public_contract.py` change
  together.

### 3.6 CLI data operations

`music-friend data inbox duplicates [--merge --yes]` (in `runtimes/cli.py`, logic in
`tools/inbox_maintenance.py`):

- Dry run (default) lists candidate pairs of distinct `releases` rows that the ladder would
  now merge (tiers 1-2 and 4, using stored state only; no network), with the tier and the two
  inbox states.
- `--merge` merges each pair into the release with the earlier `first_seen_at`: provenance
  moved, signals re-pointed, the two inbox entries collapsed with the same most-decided rule as
  the migration, loser recorded in `inbox_entry_merges` and the release pair in
  `release_merges (loser_release_local_id, winner_release_local_id, merged_at)`.
- `music-friend data inbox unmerge --yes` applies the state-restore semantics of section 3.1.
- Both are CLI-only, consistent with "MCP never performs restores, backups or deletion".

### 3.7 Refresh concurrency

- `mcp_stdio.py`: `refresh_music` runs `refresh_once` via `asyncio.to_thread` on a
  `MusicFriendApplication` built from a *second* `Catalog.open(catalog_path)` owned by the
  worker thread and closed when the run ends. `Catalog` instances are not shared across threads
  (`_transaction_depth` is per instance; `sqlite3` default `check_same_thread=True` stays).
  `Catalog.open`'s parent-directory `flock` is released when `parent_fd` is closed after
  migrations (`catalog.py:566-571`), so a second in-process open does not block.
- WAL is already on (`catalog.py:561`); `busy_timeout` already set. Readers never wait on the
  writer; the only MCP write during a refresh (`update_inbox_item`) is one short
  `BEGIN IMMEDIATE` that waits at most `busy_timeout` for the refresh's current per-artist
  transaction.
- Refresh writes stay short: one transaction per artist (`_discover_artist`), one per
  observation inside repair. No transaction spans a provider request (already true; a test
  asserts it with a fake source that fails if `in_transaction` is set on entry).
- The existing lock file still serializes refreshes; `music_status.refresh.running` reads it.
- CLI refresh is unchanged (single-threaded process).

### 3.8 `release_source_unmapped`

Defined as the number of `(watchlisted artist, release source)` pairs skipped in this run
because the artist has no `SourceReference` for that source; summed over the sources that ran.
`docs/limits.md` and `docs/mcp.md` state this and note that `music_status.identity` gives the
per-source picture; a test asserts the count for a two-source run with one artist unmapped on
each source equals 2.

## 4. Data flow

```
provider page ──normalize──> Release (+ external_links, barcodes)
        │
        ▼
ReleaseObservation ──> record_release_observation ──┐  one transaction
        │                                           │
        ├─ resolve_release_identity: tier1 native ──┤
        │                             tier2 links ──┤  exactly one or nothing
        │                             tier4 title ──┤
        ├─ put/merge release, attach provenance ────┤
        ├─ content_version (no provenance) ─────────┤
        ├─ signal: find_for_record or insert ───────┤  history
        └─ inbox: INSERT ... ON CONFLICT (kind, record) DO UPDATE latest_signal, updated_at
                                                    │  state never touched here
repair pass = for release in releases_without_inbox_entry: record_release_observation(stored)
              (second run: every call returns unchanged)
```

## 5. Acceptance criteria

Organized by implementing issue; each block is proposed as that issue's `## AC`. Verification
mechanisms name the test module they belong in.

### Issue A: inbox identity constraint and migration 014 (absorbs #57 core)

- [ ] `inbox_entries` has `UNIQUE (kind, record_local_id)`; inserting a second entry for one
      release fails with `IntegrityError` at the store, with no code-level check involved —
      verified by: `tests/store/test_catalog_records.py::test_second_inbox_entry_for_one_record_is_rejected_by_the_schema`
- [ ] A synthetic pre-014 catalog fixture (`tests/store/fixtures/v13_duplicate_inbox.sql`)
      containing (a) one release with two signals (v1 and v2 digests) and two inbox entries,
      one `saved` and one `unread`; (b) one release with `dismissed` and a later `saved` entry;
      (c) one event with two `unread` entries; upgrades to exactly one entry per record with
      states `saved`, `saved`, `unread` respectively, `created_at` = earliest and `updated_at`
      = latest of the group — verified by:
      `tests/store/test_migrations.py::test_014_collapses_duplicate_inbox_entries_most_decided_wins`
- [ ] Every collapsed loser is present in `inbox_entry_merges` with its original state and
      timestamps, and no `signals` row is deleted or modified by the migration — verified by:
      `tests/store/test_migrations.py::test_014_records_losers_and_preserves_signals`
- [ ] The migration is atomic: a forced failure after the collapse leaves `schema_migrations`
      at 13 and `inbox_entries` unchanged — verified by:
      `tests/store/test_migrations.py::test_014_failure_rolls_back` (pattern of
      `test_failed_migration_rolls_back_schema_and_version`)
- [ ] `Catalog.open` on SQLite older than 3.25.0 raises `CatalogUnavailableError` naming the
      minimum version — verified by:
      `tests/store/test_catalog_creation.py::test_open_rejects_sqlite_without_window_functions`
      (monkeypatched `sqlite_version_info`)
- [ ] Portable export includes `kind`, `record_local_id`, `latest_signal_local_id`; importing a
      pre-014 export (`signal_local_id` only) re-derives them and applies the collapse rule, so
      `data restore` of an old backup never raises — verified by:
      `tests/store/test_import.py::test_pre_014_export_imports_under_the_inbox_constraint`
- [ ] `CHANGELOG.md` and `docs/operations.md` tell users to run `music-friend data backup`
      before upgrading and describe what the collapse does — verified by:
      `tests/docs/test_public_contract.py` (existing command-grammar check) and review
- [ ] Synthetic data only; `python scripts/scan_public_tree.py .` passes; full gate green.

### Issue B: single write path and idempotent repair (absorbs #57 AC 1-2 and unmapped semantics)

- [ ] `record_release_observation` is the only caller of `put_signal`/`upsert_inbox_entry` for
      releases; `grep` in the architecture test finds no other call site in `src/` — verified
      by: `tests/architecture/test_dependencies.py::test_release_inbox_writes_go_through_record_release_observation`
- [ ] Reproduction of #57 at the entry point: a release with one signal and one inbox entry
      gains a second source reference through a cross-source merge; the next two refreshes
      (`cli.run_cli refresh releases` and MCP `refresh_music`) report `signals_created == 0`,
      `signals_repaired == 0`, inbox count unchanged, and `explain_inbox_item.sources == 2` —
      verified by: `tests/tools/test_refresh.py::test_attached_source_reference_never_creates_a_second_inbox_item`
- [ ] Crash recovery still works: a discovery committed without its signal/inbox row gets
      exactly one signal and one inbox entry on the next run, and a third run changes nothing —
      verified by: `tests/tools/test_refresh.py::test_repair_records_missing_inbox_entry_once`
- [ ] `_content_version` excludes `source_refs`, `canonical_url`, `observed_at` and explanation
      reasons: two `Release` values differing only in those fields hash equal; changing the date
      or title hashes different — verified by:
      `tests/tools/test_release_observation.py::test_content_version_ignores_provenance`
- [ ] A content change on a `saved` or `dismissed` item appends an `updated_release` signal,
      moves `latest_signal_local_id` and `updated_at`, and leaves `state` unchanged; no second
      item — verified by: `tests/tools/test_release_observation.py::test_content_change_updates_the_existing_item_without_changing_state`
- [ ] `_material_version`, `_legacy_v1_material_version`, `_release_repair_already_recorded`
      and `list_release_discoveries_without_current_signal` are removed — verified by: `ruff`
      (no dead code) and `grep -c` assertions in the architecture test
- [ ] `release_source_unmapped` counts `(artist, source)` pairs summed over the sources that
      ran; documented in `docs/limits.md` — verified by:
      `tests/tools/test_refresh.py::test_release_source_unmapped_counts_artist_source_pairs`
      and `tests/docs/test_public_contract.py`
- [ ] Events use the same `upsert_inbox_entry`; an event re-observed with unchanged content is
      `unchanged` — verified by: `tests/tools/test_event_discovery.py::test_event_reobservation_is_unchanged`
- [ ] Full gate green; synthetic data only.

### Issue C: identity ladder with MusicBrainz links and title-key folding (absorbs #56)

- [ ] Tier 1: a candidate whose `(source, native_id)` is already attached to a release merges
      into it without consulting the title key — verified by:
      `tests/tools/test_release_observation.py::test_native_id_match_wins_before_title_key`
- [ ] Tier 2: `MusicBrainzSource.recent_releases` requests
      `release?release-group=<rgid>&inc=url-rels` once per new release group (fixture from a
      recorded public response with `free streaming` relations to synthetic Spotify and Deezer
      album URLs) and emits `external_links` with `IdentityConfidence.EXTERNAL_ID`; the Deezer
      album observed later hits tier 1; the reverse order (Deezer first) merges at tier 2 —
      verified by: `tests/providers/musicbrainz/test_source.py::test_recent_releases_harvests_release_url_rels`
      and `tests/tools/test_release_observation.py::test_external_link_merges_in_either_arrival_order`
- [ ] Ambiguity: a key resolving to two distinct releases creates no release, no inbox item,
      one `identity_ambiguous` observation and increments the run metric — verified by:
      `tests/tools/test_release_observation.py::test_ambiguous_identity_stays_separate`
- [ ] Same-source conflict guard: a candidate never merges into a release carrying a different
      native id for the candidate's own source — verified by:
      `tests/tools/test_release_observation.py::test_never_merges_two_native_ids_of_one_source`
- [ ] #56: curly vs straight apostrophe, en/em dash vs hyphen, NFD vs NFC, and double spaces
      give one release with two source references; `Stop` vs `Stops` and different accented
      letters stay separate; display titles unchanged — verified by:
      `tests/store/test_catalog_records.py::test_title_key_folds_typographic_punctuation` and
      `::test_title_key_keeps_real_differences`
- [ ] Request budget: a run with N new release groups makes exactly N harvest requests, each
      paced through the existing `_PacedSource`; a run that hits the deadline leaves
      `release_link_harvest_pending` set and the next run harvests it — verified by:
      `tests/tools/test_refresh.py::test_link_harvest_is_bounded_and_resumable`
- [ ] `docs/limits.md` request table updated; full gate green.

### Issue D: `data inbox duplicates` and `unmerge` (absorbs #57 AC 3)

- [ ] `data inbox duplicates` is dry-run by default, makes no network request, lists release
      pairs with tier and both inbox states, and writes nothing — verified by:
      `tests/runtimes/test_cli_data.py::test_inbox_duplicates_dry_run_writes_nothing`
- [ ] `--merge --yes` merges each pair into the earlier release, collapses inbox entries
      most-decided-wins, and records rows in `release_merges` and `inbox_entry_merges`; a second
      run lists nothing — verified by:
      `tests/runtimes/test_cli_data.py::test_inbox_duplicates_merge_is_idempotent_and_audited`
- [ ] `data inbox unmerge --yes` restores a loser's more recent decided state onto the winner
      and prints each change — verified by: `tests/runtimes/test_cli_data.py::test_inbox_unmerge_restores_state`
- [ ] Command grammar in `_USAGE`, `docs/operations.md`; `tests/docs/test_public_contract.py`
      passes.

### Issue E: refresh does not block MCP reads (absorbs #57 AC 4)

- [ ] With a fake source that blocks for 2 s per artist, `list_inbox` and `update_inbox_item`
      complete in under 500 ms while `refresh_music` is running in the same server process —
      verified by: `tests/mcp/test_catalog_server.py::test_read_tools_respond_during_refresh`
- [ ] The refresh worker uses its own `Catalog` connection; the server connection is never
      touched from the worker thread (assert with `sqlite3.ProgrammingError` not raised and a
      thread-identity check in a test double) — verified by:
      `tests/runtimes/test_mcp_stdio.py::test_refresh_runs_on_a_worker_connection`
- [ ] No refresh transaction is open across a provider request — verified by:
      `tests/tools/test_refresh.py::test_no_transaction_spans_a_source_call`
- [ ] `music_status.refresh.running` is `true` during a run and `false` after; `_TOOL_SCHEMAS`,
      `docs/mcp.md`, skill and contract test updated — verified by:
      `tests/mcp/test_catalog_server.py::test_status_reports_running_refresh` and
      `tests/docs/test_public_contract.py`
- [ ] A second `refresh_music` during a run returns `already_running` with `retry_after`
      (existing behavior preserved) — verified by existing test plus the threaded variant.

## 6. Out of scope, future PRs, deferred

**Out of scope for these PRs**

- Re-surfacing a `dismissed` item as `unread` when its release content changes. Product
  decision; the design keeps `state` untouched on update.
- Merging *artists* across sources; artist identity mapping is #41's and unchanged.
- Rewriting historical `signals.material_version` values.

**Future PRs**

- Tier 3 barcode matching (`release_identifiers` table, Deezer `GET /album/<id>` for `upc`,
  MusicBrainz release `barcode` from the tier-2 harvest). Trigger: `identity_ambiguous` or
  `identity_conflict` metrics non-zero on the owner's library over two weeks after Issue C.
- A `music_status` field summarizing `identity_conflict` counts for agent-driven cleanup.

**Deferred decisions**

- Whether `release_discoveries` should become per `(release, source)` rows; unnecessary once
  identity lookups move to `record_sources`.

## 7. Risks

- **Migration collapse picks the wrong winner.** Mitigated by the audit table, the pre-upgrade
  backup instruction, `unmerge`, and the synthetic fixture mirroring the observed patterns.
- **Tier 2 false merge from a wrong url-rel in MusicBrainz.** Editors do mislink. Mitigated by
  the same-source conflict guard and exactly-one rule; a mislink that points one release group
  at another source's *only* album still merges. Accepted: it is far rarer than title
  collisions, and `data inbox duplicates`' reverse (`release_merges`) makes it recoverable.
- **Second in-process connection.** Two connections in one process is new for this codebase.
  Mitigated by WAL (already on), per-thread `Catalog` instances, short transactions, and the
  existing lock file; the tests in Issue E exercise the concurrent path.

## 8. Prior art

- **MusicBrainz** separates *release group* (the album as a work of publication), *release*
  (an edition: country, label, barcode, medium) and *recording* (ISRC lives here). Streaming
  links are release-level url-relationships (`free streaming`, `streaming`,
  `purchase for download`); release groups carry database and review links only. Verified live
  on 2026-09-26 with `GET /ws/2/release?release-group=<rgid>&inc=url-rels`,
  `GET /ws/2/release-group/<rgid>?inc=url-rels` (no streaming rels) and
  `GET /ws/2/url?resource=<deezer album url>&inc=release-rels` (resolves to the release).
  `release?query=barcode:<upc>` resolves a barcode to a release and its release group.
  This is why the design keys the *canonical release* to a release group but harvests links
  and barcodes from its releases.
- **Deezer** exposes `upc` on `GET /album/<id>` but not on `GET /artist/<id>/albums` (verified
  live 2026-09-26), which is why tier 3 costs one request per album and is deferred.
- **beets, Lidarr, ListenBrainz**: see the researcher findings appended in section 9 with
  sources; in short, all three key album identity on the MusicBrainz release group (or a
  canonical release redirect) and treat provider ids as attached metadata, which is the
  precedence this design adopts.

## 9. Research notes and sources

_Populated from the researcher report; see the review trail for provenance._

## Review trail

_Cross-family adversarial round recorded here after dispatch (author: designer, Claude;
reviewer: codex-dev, Codex)._
