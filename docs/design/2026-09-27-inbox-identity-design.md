# Inbox identity: one inbox item per release, by construction

Status: draft for review, revised after the cross-family adversarial round (see
`## Review trail`). Supersedes the dedupe patches in #47, #53 and #55 as the model of record;
absorbs #56 and #57; states the disposition of PR #59. Companion to
[2026-09-24-release-source-design.md](2026-09-24-release-source-design.md).

All titles, artists and identifiers in this document are synthetic.

## 1. Problem

Duplicate inbox items keep appearing, and every fix so far has patched one duplicate pattern
after it was observed on a real library:

| Pattern | Seen | Patched by |
|---|---|---|
| Same-source repeat: one release surfaced again on a later run | #50 | #53 (content-only `material_version` plus a v1 replay) |
| Cross-source title variants: `(Remix)` vs `(X Remix)`, `(from Some Film: The Album)` vs plain, partial artist credits | #50, #54 | #53, #55 (title-key folding) |
| Curly vs straight apostrophe across sources | #56 | PR #59 (open) |
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
inside the `refresh_music` handler, on the event loop, on the server's only catalog connection:
`catalog_server.py:583-591` and `:939-946`, `mcp_stdio.py:176-262`), and the
`release_source_unmapped` metric's meaning in a multi-source run is undefined.

**Goal:** A release that the catalog already knows never produces a second inbox item, whatever
path (discovery, repair, a new source, an upgrade) observes it again, and the store enforces
that rather than code.

## 2. Approach

Make identity explicit at three layers and let the database hold the invariant:

1. **Inbox identity = inbox subject.** A new `inbox_subjects` row stands for one real-world
   release (or one event). `releases` rows map many-to-one onto a subject; `inbox_entries` is
   `UNIQUE (kind, subject_local_id)`. An inbox item is "the user's standing decision about this
   subject". Signals become the item's *history*; an item points at its latest signal. Two
   `releases` rows that later turn out to be the same real-world release are reconciled by
   re-pointing one row's `subject_local_id` (a *subject merge*); release `local_id`s never change
   and nothing is deleted. No code path can create a second item for a subject because the
   insert fails; the write path uses `ON CONFLICT DO UPDATE` to treat re-observation as an
   update.
2. **Release identity = a resolution ladder, strong keys first, provisional until
   corroborated.** Native ids already attached to a release (`source_references` is
   `UNIQUE (source, native_id)`), then cross-source links harvested from MusicBrainz release
   url-relationships (verified live: release-level `free streaming` / `streaming` relations
   point at Spotify and Deezer album URLs), then, as a later tier, barcodes, and only last the
   existing conservative title key. Every tier merges only when it identifies exactly one
   subject. A harvested link is *provisional* until the linked provider's own report
   corroborates it; a contradiction is a recorded conflict, never an automatic merge.
3. **One write path.** `record_release_observation` is the only function that turns an observed
   release into store state (release, subject, provenance, discovery, signal, inbox). Discovery
   and repair both call it, so repair is a re-observation of stored state and is idempotent by
   construction: the second call returns `unchanged`.

Provenance is not content. The content digest that decides "did this release change" hashes
title, type, date, precision and credited artists; never `source_refs`, never the explanation.
Attaching a source is recorded as provenance (a `source_references` row plus an observation),
not as a change the user needs to see.

Existing duplicates are collapsed by a migration under the new constraint (most-decided state
wins), with pre-merge snapshots of every row touched so the collapse is reversible by
compare-and-swap, and a CLI dry-run/merge operation handles the one class the migration must
not decide on its own: two distinct `releases` rows that are the same real-world release.

Refresh runs off the event loop on a worker thread with its own SQLite connection; MCP reads
are plain autocommit `SELECT`s on the server's connection and, in WAL mode, never wait on the
writer. Writer lock hold time is bounded by construction (no transaction spans a provider
request; one transaction per artist).

### 2.1 Disposition of PR #59

PR #59 (head `ea6d51b`) should **merge as-is now**; it fixes live pain and every part of it is
compatible with this design, which then absorbs it:

- Its typographic/Unicode `_normalized_title` folding becomes the tier-4 key. Issue C moves the
  fold into one shared `fold_title_key()` used by both `_normalized_title` and
  `_release_title_variant_key` (the deviation #59 documents: `catalog.py` has no
  `_normalized_title`, so the two key functions currently fold differently).
- `music-friend data dedupe-inbox [--apply]` (dry-run; `--apply` dismisses extras, never
  deletes) is superseded once migration 014 collapses same-subject duplicates structurally.
  Issue D replaces it with `data inbox duplicates` / `data inbox unmerge` (section 3.6) and
  removes `dedupe-inbox` with a CHANGELOG note; until then it is the right stopgap.
- The `doctor` release-source listing fix is unrelated to this design and stands.

## 3. Components

### 3.1 Schema, migration `014_inbox_identity.sql`

`src/music_friend/store/schema/014_inbox_identity.sql`, registered in `migrations.py`.

```sql
-- 1. Subjects: one row per real-world release or event. Releases map many-to-one.
CREATE TABLE inbox_subjects (
    local_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('release', 'event')),
    created_at TEXT NOT NULL
);
ALTER TABLE releases ADD COLUMN subject_local_id TEXT REFERENCES inbox_subjects(local_id);
-- Backfill: every existing release is its own subject (subject id = release id).
INSERT INTO inbox_subjects (local_id, kind, created_at)
    SELECT local_id, 'release', observed_at FROM releases;
UPDATE releases SET subject_local_id = local_id;
-- Events are 1:1 with their subject; the subject id is the event id (no column needed).
INSERT INTO inbox_subjects (local_id, kind, created_at)
    SELECT local_id, 'event', observed_at FROM events;

-- 2. Snapshots first, so every collapse below is reversible.
CREATE TABLE inbox_entry_snapshots (      -- every row touched by a merge, as it was
    snapshot_id INTEGER PRIMARY KEY,
    merge_id TEXT NOT NULL,               -- '014' for the migration, else the CLI op id
    role TEXT NOT NULL CHECK (role IN ('winner_before', 'loser')),
    local_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_local_id TEXT NOT NULL,
    signal_local_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    winner_local_id TEXT NOT NULL,
    winner_updated_at_after TEXT NOT NULL,   -- CAS token for unmerge (section 3.6)
    merged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE subject_merges (             -- release re-pointing, for the CLI merge only
    merge_id TEXT NOT NULL,
    release_local_id TEXT NOT NULL,
    from_subject_local_id TEXT NOT NULL,
    to_subject_local_id TEXT NOT NULL,
    merged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (merge_id, release_local_id)
);

-- 3. Rebuild inbox_entries with subject identity (SQLite cannot ADD a UNIQUE constraint).
CREATE TABLE inbox_entries_v2 (
    local_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('release', 'event')),
    subject_local_id TEXT NOT NULL REFERENCES inbox_subjects(local_id),
    latest_signal_local_id TEXT NOT NULL REFERENCES signals(local_id),
    state TEXT NOT NULL CHECK (state IN ('unread', 'saved', 'dismissed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (kind, subject_local_id)
);

-- 4. Collapse: one winner per (kind, subject). Rank: decided beats unread; among decided,
--    the most recently updated wins; ties break on local_id. The winner keeps its own
--    local_id and state; created_at = earliest in the group; updated_at = latest in the
--    group; latest_signal = the group's signal with the greatest observed_at (tie: local_id).
WITH ranked AS (
    SELECT e.local_id, s.kind,
           CASE s.kind WHEN 'release' THEN r.subject_local_id ELSE s.record_local_id END
               AS subject_local_id,
           e.signal_local_id, e.state, e.created_at, e.updated_at, s.observed_at,
           ROW_NUMBER() OVER (PARTITION BY s.kind, s.record_local_id
               ORDER BY CASE e.state WHEN 'unread' THEN 1 ELSE 0 END,
                        e.updated_at DESC, e.local_id) AS rank,
           MIN(e.created_at) OVER (PARTITION BY s.kind, s.record_local_id) AS first_created,
           MAX(e.updated_at) OVER (PARTITION BY s.kind, s.record_local_id) AS last_updated,
           FIRST_VALUE(e.signal_local_id) OVER (PARTITION BY s.kind, s.record_local_id
               ORDER BY s.observed_at DESC, s.local_id) AS head_signal
    FROM inbox_entries AS e
    JOIN signals AS s ON s.local_id = e.signal_local_id
    LEFT JOIN releases AS r ON r.local_id = s.record_local_id
)
INSERT INTO inbox_entries_v2
SELECT local_id, kind, subject_local_id, head_signal, state, first_created, last_updated
FROM ranked WHERE rank = 1;
-- Snapshots: every loser, and every winner whose row changed (rank = 1 in a group of > 1).
INSERT INTO inbox_entry_snapshots (merge_id, role, local_id, kind, subject_local_id,
    signal_local_id, state, created_at, updated_at, winner_local_id, winner_updated_at_after)
SELECT '014', CASE WHEN l.rank = 1 THEN 'winner_before' ELSE 'loser' END, l.local_id, l.kind,
       l.subject_local_id, l.signal_local_id, l.state, l.created_at, l.updated_at,
       w.local_id, w.last_updated
FROM ranked AS l JOIN ranked AS w
  ON w.kind = l.kind AND w.subject_local_id = l.subject_local_id AND w.rank = 1
WHERE EXISTS (SELECT 1 FROM ranked AS o
              WHERE o.kind = l.kind AND o.subject_local_id = l.subject_local_id AND o.rank > 1);

DROP TABLE inbox_entries;
ALTER TABLE inbox_entries_v2 RENAME TO inbox_entries;
CREATE INDEX inbox_entries_state_order ON inbox_entries (state, updated_at DESC, local_id);
CREATE INDEX releases_subject ON releases (subject_local_id);
```

Notes:

- The migration collapses only *same-record* duplicates (the #57 shape). It never decides that
  two `releases` rows are one real-world release; that is the CLI's job (section 3.6) because
  it is a judgment the user can review in a dry run.
- Window functions need SQLite 3.25 (2018). The migration test asserts
  `sqlite3.sqlite_version_info >= (3, 25, 0)`; `Catalog.open` raises `CatalogUnavailableError`
  naming the minimum below it, before applying migrations.
- Signals are never deleted or rewritten. A loser's signal stays reachable from
  `explain_inbox_item` as history (section 3.5).
- Loser inbox `local_id`s stop resolving (`not_found`). They are conversational handles from
  `list_inbox`, not durable references; `docs/mcp.md` already says to take the id from a fresh
  listing. This is the one thing unmerge does not restore (section 3.6).
- `signals` keeps its `UNIQUE (provider, kind, provider_native_id, material_version)`; the
  meaning of `material_version` becomes a content digest (section 3.3). Existing rows are not
  rewritten; the write path never needs to reproduce an old digest because it checks the inbox
  by subject, not the signal by digest.
- `release_discoveries` stays for cursors and `first_seen_at`/`last_seen_at` and stops being an
  identity lookup.
- Before upgrading: the CHANGELOG entry and `docs/operations.md` say to run
  `music-friend data backup`. `data restore` of a pre-014 backup into a 014 catalog goes
  through the portable importer, which re-derives subjects and applies the same collapse rule,
  so restore never violates the constraint.

### 3.2 Domain

`src/music_friend/domain/models.py`:

- `Release` gains `subject_local_id: RecordId` (defaults to `local_id` for new releases).
- `InboxEntry` gains `kind: SignalKind`, `subject_local_id: RecordId`; `signal_local_id` is
  renamed `latest_signal_local_id` (portable export version bumps; importer accepts both).
- `IdentityConfidence` gains `PROVISIONAL` (a harvested cross-source link the linked provider
  has not yet corroborated). Ordering: `SOURCE_ONLY < PROVISIONAL < EXTERNAL_ID <
  USER_CONFIRMED`.
- New frozen dataclasses in `domain/observations.py`:
  - `ReleaseObservation(release, source, native_id, monitored_artist_local_id,
    external_links: tuple[SourceReference, ...], barcodes: tuple[str, ...], observed_at)`;
    `external_links` are the cross-source native ids the source itself asserts (MusicBrainz
    url-rels), each `PROVISIONAL`.
  - `ObservationOutcome(kind: Literal["created", "updated", "unchanged", "provenance_attached",
    "ambiguous", "conflict"], release_local_id, subject_local_id, inbox_local_id | None,
    method: IdentityMethod | None)`.
  - `IdentityMethod` enum: `native_id`, `external_link`, `barcode`, `title_key`, `none`.

### 3.3 Single write path

`src/music_friend/tools/release_observation.py` (application layer; imports store and domain,
never providers or MCP, per `tests/architecture/test_dependencies.py`).

```
record_release_observation(catalog, observation) -> ObservationOutcome
  with catalog.transaction():                       # one BEGIN IMMEDIATE, no I/O inside
    match = resolve_release_identity(catalog, observation)      # section 3.4
    if match.ambiguous or match.conflict:
        catalog.put_observation(fact 'identity_ambiguous' | 'identity_conflict')
        if match.conflict: detach the contradicted provisional link (section 3.4)
        create the candidate as its own release + subject; continue as "created"
    elif match.release_local_id is None:
        catalog.put_release(observation.release)   # new release, subject = its own id
    else:
        catalog.attach_source_references(match.release_local_id,
            (observation source ref,) + observation.external_links)   # idempotent
        promote the matched provisional ref to EXTERNAL_ID (corroborated)
        catalog.merge_release_content(match.release_local_id, observation.release)  # 3.3.1
    subject = catalog.get_release(release_local_id).subject_local_id
    catalog.touch_release_discovery(release_local_id, source, native_id, observed_at)
    content_version = _content_version(catalog.get_release(release_local_id))
    signal = catalog.find_signal_for_record(RELEASE, release_local_id, content_version)
    inbox = catalog.get_inbox_entry_for_subject(RELEASE, subject)
    if signal is None:
        reason = NEW_RELEASE if inbox is None else UPDATED_RELEASE
        signal = catalog.put_signal(Signal(..., material_version=content_version, reason))
    if inbox is None:
        catalog.upsert_inbox_entry(RELEASE, subject, signal.local_id, UNREAD)   # created
    elif inbox.latest_signal_local_id != signal.local_id:
        catalog.upsert_inbox_entry(RELEASE, subject, signal.local_id, inbox.state)  # updated
    else: unchanged / provenance_attached
```

`_content_version(release)` hashes `{"version": 3, "title", "release_type", "release_date",
"date_precision", "artist_refs"}` only. `source_refs`, `canonical_url`, `observed_at`,
`subject_local_id` and the explanation are excluded by construction. `refresh.py`'s
`_material_version`, `_legacy_v1_material_version`, `_release_material`,
`_release_repair_already_recorded` and the release half of `_repair_missing_signals` are
deleted, together with `Catalog.list_release_discoveries_without_current_signal`.

`Catalog.upsert_inbox_entry` is the only inbox insert in the codebase:

```sql
INSERT INTO inbox_entries (local_id, kind, subject_local_id, latest_signal_local_id, state,
                           created_at, updated_at)
VALUES (?, ?, ?, ?, 'unread', ?, ?)
ON CONFLICT (kind, subject_local_id) DO UPDATE SET
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
| Second source attaches (the #57 case) | `source_references` + `record_sources` rows; `observations` row `source_attached` | nothing (`provenance_attached`); `explain_inbox_item.sources` becomes 2 |
| Content changes (date correction, title fix) | new signal with `updated_release`; inbox `latest_signal_local_id`, `updated_at` move; `state` unchanged | the same item with a newer `updated_at` and `updated_release` in `reasons`; not a new item, not resurrected from `dismissed` |
| Genuinely different release (deluxe, other remixer) | new release, new subject, new item | a new item |

`merge_release_content` is deterministic and recorded. Precedence for each field is the
configured `release_sources` order at write time (`title`, `release_type`): the earlier source
wins whenever it has reported the release, regardless of arrival order, so a Deezer-first title
is replaced when MusicBrainz reports the same release later, and never the reverse. For
`release_date`/`date_precision` the *more precise* value wins (DAY over MONTH over YEAR); on
equal precision, `release_sources` order. `artist_refs` is the union in stable first-seen
order. Every overruled value writes an `observations` row `content_conflict:<field>` against
the losing source's reference, so a disagreement is inspectable without being a user-visible
change. Whether a content change should re-surface a `dismissed` item is a product decision and
out of scope (section 6); the design keeps `state` untouched.

### 3.4 Identity resolution ladder

`resolve_release_identity(catalog, observation) -> IdentityMatch(release_local_id | None,
method, ambiguous, conflict)` in `tools/release_observation.py`; SQL in `store/catalog.py`.

| Tier | Key | Lookup | Merge when | Verified availability |
|---|---|---|---|---|
| 1 | `(source, native_id)` of the observation | `record_sources JOIN source_references` (`UNIQUE (source, native_id)`) | exactly one release, and the matched reference is not `PROVISIONAL`; a `PROVISIONAL` hit goes through corroboration (below) | Structural; already stored for every release |
| 2 | External links asserted by the source | each `(source, native_id)` in `observation.external_links` looked up as tier 1 | all links resolve to the same one release | MusicBrainz `GET /ws/2/release?release-group=<rgid>&inc=url-rels` returns per-release `free streaming`/`streaming` relations to `open.spotify.com/album/<id>` and `www.deezer.com/album/<id>`; release-group-level url-rels carry none (live probe 2026-09-26) |
| 3 (later) | Barcode (UPC/EAN) | `release_identifiers (scheme, value)` | exactly one release; digits only, leading zeros stripped, 11-14 digits | MusicBrainz release `barcode` comes in the tier-2 request; Deezer `GET /album/<id>` has `upc` but `GET /artist/<id>/albums` does not; Spotify `GET /albums/<id>` has `external_ids.upc` but `GET /artists/<id>/albums` does not (verified, section 9) |
| 4 | Conservative title key | `find_release_by_title_key` over `releases` + `release_artists` (generalized `find_release_discovery_variant`) | exactly one release; artist-set overlap; same date (or +-1 day when both DAY) | Existing (#42, #50, #54) plus PR #59's punctuation folding |

Rules that hold at every tier:

- **Exactly one or nothing.** A key that resolves to two or more distinct subjects is
  `ambiguous`: the candidate becomes its own release and subject, an `observations` row
  `identity_ambiguous` is written and the `release_identity_ambiguous` run metric increments.
  A false merge hides a real release, which is worse than a duplicate.
- **Provisional links must be corroborated.** A harvested link is stored `PROVISIONAL`. When
  the linked provider later reports that native id (a tier-1 hit on a `PROVISIONAL`
  reference), the report must be *compatible* with the release it would join: the tier-4 key
  agrees (title variants compatible and date within the tier-4 tolerance) or any other
  non-provisional key agrees. Compatible: the reference is promoted to `EXTERNAL_ID` and the
  merge proceeds. Incompatible: the provisional reference is detached from the release it was
  harvested onto, the candidate becomes its own release and subject, an `identity_conflict`
  observation is written and the `release_identity_conflict` metric increments. A
  valid-but-wrong url-rel therefore costs at worst one extra (correct) item, never a hidden
  release. `update_watchlist`-style user confirmation for releases is out of scope; the CLI
  merge (section 3.6) is the user's override.
- **Same-source conflict guard.** A candidate never merges into a release that already carries a
  *different* non-provisional native id for the candidate's own source (two Deezer album ids
  are two editions; deluxe vs standard is the typical case), whatever a lower tier says.
- **Tiers do not vote.** The first tier that returns exactly one release decides; lower tiers
  are not consulted, except as the corroboration check for a provisional hit.
- **Late links never auto-merge subjects.** If a tier-2 link points at a release whose subject
  differs from the subject that tier 1 (or the same-source guard) already established for the
  candidate, both releases already exist; the write path records `identity_conflict` and does
  not re-point either subject. The pair appears in `data inbox duplicates` for the user to
  merge (section 3.6). This is the deliberate trade: a residual duplicate is visible and
  reversible; an automatic subject merge on one editor's link would not be.
- **Title-key folding (#56 / PR #59).** `_normalized_title` and `_release_title_variant_key`
  share one `fold_title_key()` in `store/catalog.py`: NFKC, U+2018/2019/201B/2032 -> `'`,
  U+201C/201D/2033 -> `"`, U+2010..U+2015 and U+2212 -> `-`, U+2026 -> `...`, casefold,
  whitespace collapse. Display titles are never altered.

Order of arrival:

- *MusicBrainz first, Deezer later* (common): the MusicBrainz observation attaches a
  `PROVISIONAL` Deezer reference; Deezer's own report hits tier 1, passes corroboration
  (same title key and date), and the reference is promoted. No title guess was needed for the
  merge itself.
- *Deezer first, MusicBrainz later* (the timeliness case Deezer exists for): Deezer creates the
  release and subject. When MusicBrainz's release group arrives with a url-rel to that Deezer
  album, tier 2 finds the Deezer-created release (a non-provisional reference, since Deezer
  reported it itself) and merges into it. If editors have not linked it yet, tier 4 applies; if
  tier 4 misses, two subjects exist until a later harvest links them, which is the late-link
  case above: recorded, surfaced, user-merged.

Harvest cost: one MusicBrainz request per *new* release group (paced at 1/s inside the
existing `_PacedSource`), bounded per run by the existing deadline; a release group whose
harvest did not run is harvested on the next run (`link_harvest_pending` flag on
`release_discoveries`). Tier 3 is designed here and deferred (section 6) until the
`identity_ambiguous`/`identity_conflict` metrics show tiers 2 and 4 leave a measurable gap.

### 3.5 MCP and application surface (provider-neutral)

- `list_inbox` items: unchanged fields (`updated_at` already present); `summary` unchanged.
- `explain_inbox_item`: `reasons` stays (latest signal); adds `history`: the subject's signals
  oldest-first as `{observed_at, reasons}`, and `sources`: the count of source references
  across the subject's releases (no provider ids, per `tests/mcp/test_catalog_server.py`).
- `music_status`: `refresh.running: bool` (from the lock file lease) and
  `identity.conflicts: int` (open `identity_conflict` observations) so an agent can avoid a
  second refresh and can tell the user a cleanup is available.
- `_TOOL_SCHEMAS`, `docs/mcp.md`, the skill and `tests/docs/test_public_contract.py` change
  together.

### 3.6 CLI data operations

`music-friend data inbox duplicates [--merge --yes]` (grammar in `runtimes/cli.py`, logic in
`tools/inbox_maintenance.py`); replaces PR #59's `data dedupe-inbox`.

- Dry run (default) lists pairs of releases in *different* subjects that the ladder would now
  join (tiers 1-2 and 4 over stored state; no network), plus every recorded
  `identity_conflict`, with the tier, both titles and both inbox states. Writes nothing.
- `--merge --yes` performs a subject merge per pair: the release with the later `first_seen_at`
  is re-pointed to the other's subject (`subject_merges` row); the two inbox entries collapse
  with the migration's rule (most-decided wins; head signal = greatest `observed_at`);
  snapshots of the loser and of the winner-before are written to `inbox_entry_snapshots` with
  `winner_updated_at_after` set to the winner's post-merge `updated_at`; the `identity_conflict`
  observation is closed. Provisional references involved are promoted to `USER_CONFIRMED`.
- `music-friend data inbox unmerge <merge_id> --yes` is compare-and-swap recovery. For each
  snapshot in the merge: if the winner's current `updated_at` equals the recorded
  `winner_updated_at_after` (no decision since the merge), restore `winner_before`'s state,
  `latest_signal_local_id` and timestamps, and re-point each `subject_merges` release back to
  its `from_subject_local_id`; if the winner has been touched since, skip that winner and print
  why. Losers' `inbox_entries` rows are re-inserted only when re-pointing restored their subject
  (CLI merges); for the migration's same-subject losers no second row can exist, so their
  snapshot is reported, not re-inserted. Recoverable: every pre-merge state, head signal and
  timestamp, and every release-to-subject mapping. Not recoverable: a loser inbox `local_id` as
  a live handle when its subject still has a winner, and any decision the user made after the
  merge (by design, CAS refuses to overwrite it).
- Both are CLI-only, consistent with "MCP never performs restores, backups or deletion".

### 3.7 Refresh concurrency

Async boundary. `catalog_server.py`'s `refresh_music` handler awaits
`asyncio.to_thread(refresh, kind, force)`; `_safe_call` gains an async twin
(`_safe_call_async`) so the error mapping is unchanged. Every other tool stays a synchronous
handler on the event loop, as today, because its work is a handful of indexed `SELECT`s.

Connections. The worker thread builds its own `MusicFriendApplication(Catalog.open(path))`
and closes it when the run ends; the server's connection is never touched from the worker.
`Catalog` instances are not shared across threads (`_transaction_depth` is per instance;
`sqlite3`'s default `check_same_thread=True` stays). `Catalog.open`'s parent-directory
`flock` is released when `parent_fd` is closed after migrations (`catalog.py:566-571`), so a
second in-process open does not block.

What can and cannot wait:

- **MCP reads** (`list_inbox`, `explain_inbox_item`, `music_status`, `search_catalog`,
  `list_watchlist`, `summarize_listening_history`) run in autocommit outside `BEGIN IMMEDIATE`.
  In WAL mode a reader never waits on the writer, so they return in milliseconds while the
  refresh writes. The sub-500 ms AC applies to these.
- **MCP writes** (`update_inbox_item`, `update_watchlist`, `update_setup`) take one short
  `BEGIN IMMEDIATE`. If the refresh holds the write lock at that instant, they wait for the
  refresh's *current transaction* to commit, bounded by `busy_timeout` (5 s, `catalog.py:81`).
  The design bounds that wait by bounding the writer's hold time, not by promising it is zero:
  the AC for writes is "completes within 1 s while a refresh is running", not 500 ms.
- **Writer hold time** is bounded by construction: no transaction spans a provider request
  (`_discover_artist` collects pages first, then opens the transaction; a test asserts the
  fake source is never called with `connection.in_transaction` set); one transaction per artist
  with at most `_MAX_RELEASES_PER_ARTIST` (100) observations, each a few indexed statements;
  repair uses one transaction per observation. A test measures the per-artist transaction at
  100 releases under 200 ms on CI hardware.
- The existing lock file still serializes refreshes; `music_status.refresh.running` reads it.
  CLI refresh (a separate process) and the MCP server already coexist through WAL and the lock
  file; nothing changes for the CLI.

### 3.8 `release_source_unmapped`

Defined as the number of `(watchlisted artist, release source)` pairs skipped in this run
because the artist has no `SourceReference` for that source; summed over the sources that ran.
`docs/limits.md` and `docs/mcp.md` state this and note that `music_status.identity` gives the
per-source picture; a test asserts a two-source run with one artist unmapped on each source
reports 2.

## 4. Data flow

```
provider page ──normalize──> Release (+ external_links [PROVISIONAL], barcodes)
        │
        ▼
ReleaseObservation ──> record_release_observation ──┐  one transaction, no I/O inside
        │                                           │
        ├─ resolve_release_identity: tier1 native ──┤  provisional hit -> corroborate
        │                             tier2 links ──┤  exactly one or nothing
        │                             tier4 title ──┤  late link -> conflict, no re-point
        ├─ put/merge release, attach provenance ────┤  subject = release.subject_local_id
        ├─ content_version (no provenance) ─────────┤
        ├─ signal: find_for_record or insert ───────┤  history
        └─ inbox: INSERT ... ON CONFLICT (kind, subject) DO UPDATE latest_signal, updated_at
                                                    │  state never touched here
repair pass = for subject in subjects_without_inbox_entry: record_release_observation(stored)
              (second run: every call returns unchanged)
cleanup      = data inbox duplicates [--merge]: subject merge, snapshots, CAS unmerge
```

## 5. Acceptance criteria

Organized by implementing issue; each block is proposed as that issue's `## AC`. Verification
mechanisms name the test module they belong in.

### Issue A: inbox subjects, constraint and migration 014 (absorbs #57 core)

- [ ] `inbox_entries` has `UNIQUE (kind, subject_local_id)`; inserting a second entry for one
      subject fails with `IntegrityError` at the store with no code-level check involved —
      verified by: `tests/store/test_catalog_records.py::test_second_inbox_entry_for_one_subject_is_rejected_by_the_schema`
- [ ] Every release has a `subject_local_id`; after 014 it equals the release `local_id`; two
      releases may share one subject and `get_inbox_entry_for_subject` returns the single entry
      for both — verified by: `tests/store/test_catalog_records.py::test_releases_map_many_to_one_onto_subjects`
- [ ] A synthetic pre-014 fixture (`tests/store/fixtures/v13_duplicate_inbox.sql`) with (a) one
      release, two signals (v1 and v2 digests), two inbox entries `saved` + `unread`; (b) one
      release with `dismissed` then a later `saved` entry; (c) one event with two `unread`
      entries; (d) one release with a single entry; upgrades to exactly one entry per subject
      with states `saved`, `saved`, `unread`, unchanged; `created_at` = earliest and
      `updated_at` = latest of each group; `latest_signal_local_id` = the group's signal with
      the greatest `observed_at` — verified by:
      `tests/store/test_migrations.py::test_014_collapses_duplicate_inbox_entries_most_decided_wins`
- [ ] For every collapsed group, `inbox_entry_snapshots` holds one `winner_before` row and one
      `loser` row per loser with original state and timestamps and
      `winner_updated_at_after` equal to the winner's new `updated_at`; case (d) writes no
      snapshot; no `signals` row is deleted or modified — verified by:
      `tests/store/test_migrations.py::test_014_snapshots_every_touched_row_and_preserves_signals`
- [ ] The migration is atomic: a forced failure after the collapse leaves `schema_migrations`
      at 13 and `inbox_entries` unchanged — verified by:
      `tests/store/test_migrations.py::test_014_failure_rolls_back` (pattern of
      `test_failed_migration_rolls_back_schema_and_version`)
- [ ] `Catalog.open` on SQLite older than 3.25.0 raises `CatalogUnavailableError` naming the
      minimum version before applying migrations — verified by:
      `tests/store/test_catalog_creation.py::test_open_rejects_sqlite_without_window_functions`
      (monkeypatched `sqlite_version_info`)
- [ ] Portable export includes `subject_local_id` on releases and `kind`, `subject_local_id`,
      `latest_signal_local_id` on inbox entries; importing a pre-014 export re-derives them and
      applies the collapse rule, so `data restore` of an old backup never raises — verified by:
      `tests/store/test_import.py::test_pre_014_export_imports_under_the_inbox_constraint`
- [ ] `CHANGELOG.md` and `docs/operations.md` tell users to run `music-friend data backup`
      before upgrading and describe the collapse — verified by:
      `tests/docs/test_public_contract.py` (command grammar) and review
- [ ] Synthetic data only; `python scripts/scan_public_tree.py .` passes; full gate green.

### Issue B: single write path and idempotent repair (absorbs #57 AC 1-2 and unmapped semantics)

- [ ] `record_release_observation` is the only caller of `put_signal`/`upsert_inbox_entry` for
      releases; the architecture test finds no other call site in `src/` — verified by:
      `tests/architecture/test_dependencies.py::test_release_inbox_writes_go_through_record_release_observation`
- [ ] Reproduction of #57 at the entry point: a release with one signal and one inbox entry
      gains a second source reference through a cross-source merge; the next two refreshes
      (`cli.run_cli refresh releases` and MCP `refresh_music`) report `signals_created == 0`,
      `signals_repaired == 0`, inbox count unchanged, and `explain_inbox_item.sources == 2` —
      verified by: `tests/tools/test_refresh.py::test_attached_source_reference_never_creates_a_second_inbox_item`
- [ ] Crash recovery: a release committed without its signal/inbox row gets exactly one signal
      and one inbox entry on the next run, and a third run changes nothing — verified by:
      `tests/tools/test_refresh.py::test_repair_records_missing_inbox_entry_once`
- [ ] `_content_version` excludes `source_refs`, `canonical_url`, `observed_at`,
      `subject_local_id` and explanation reasons: two `Release` values differing only in those
      hash equal; changing date or title hashes different — verified by:
      `tests/tools/test_release_observation.py::test_content_version_ignores_provenance`
- [ ] A content change on a `saved` or `dismissed` item appends an `updated_release` signal,
      moves `latest_signal_local_id` and `updated_at`, leaves `state` unchanged, creates no
      second item — verified by: `tests/tools/test_release_observation.py::test_content_change_updates_the_existing_item_without_changing_state`
- [ ] `merge_release_content` is deterministic: with `release_sources = (musicbrainz, deezer)`,
      a Deezer-first title is replaced by a later MusicBrainz title and not the reverse; a DAY
      date beats a MONTH date from either source; each overruled value writes one
      `content_conflict:<field>` observation — verified by:
      `tests/tools/test_release_observation.py::test_content_merge_precedence_is_deterministic_and_recorded`
- [ ] `_material_version`, `_legacy_v1_material_version`, `_release_repair_already_recorded`
      and `list_release_discoveries_without_current_signal` are removed — verified by: `ruff`
      and `grep -c` assertions in the architecture test
- [ ] `release_source_unmapped` counts `(artist, source)` pairs summed over the sources that
      ran; documented in `docs/limits.md` — verified by:
      `tests/tools/test_refresh.py::test_release_source_unmapped_counts_artist_source_pairs`
      and `tests/docs/test_public_contract.py`
- [ ] Events use `upsert_inbox_entry` with the event id as subject; an event re-observed with
      unchanged content is `unchanged` — verified by:
      `tests/tools/test_event_discovery.py::test_event_reobservation_is_unchanged`
- [ ] Full gate green; synthetic data only.

### Issue C: identity ladder with provisional MusicBrainz links and shared title key (absorbs #56 after PR #59)

- [ ] Tier 1: a candidate whose `(source, native_id)` is already attached as a non-provisional
      reference merges without consulting the title key — verified by:
      `tests/tools/test_release_observation.py::test_native_id_match_wins_before_title_key`
- [ ] Tier 2 harvest: `MusicBrainzSource.recent_releases` requests
      `release?release-group=<rgid>&inc=url-rels` once per new release group (fixture recorded
      from a public response with `free streaming` relations rewritten to synthetic Spotify and
      Deezer album ids) and emits `external_links` with `IdentityConfidence.PROVISIONAL` —
      verified by: `tests/providers/musicbrainz/test_source.py::test_recent_releases_harvests_release_url_rels`
- [ ] Corroboration: the Deezer album observed after a MusicBrainz harvest hits the provisional
      reference, matches the title key and date, is promoted to `EXTERNAL_ID`, and produces no
      second item; the reverse arrival order (Deezer first) merges at tier 2 — verified by:
      `tests/tools/test_release_observation.py::test_external_link_merges_in_either_arrival_order`
- [ ] Valid-but-wrong url-rel: a MusicBrainz release group links to a Deezer album whose own
      report has an incompatible title and date; the provisional reference is detached, the
      Deezer release becomes its own subject with its own item, one `identity_conflict`
      observation is written, `release_identity_conflict` increments, and no release is hidden
      — verified by: `tests/tools/test_release_observation.py::test_wrong_url_rel_is_detached_not_merged`
- [ ] Late link: a tier-2 link pointing at a release in another subject records
      `identity_conflict`, re-points nothing, and the pair appears in
      `data inbox duplicates` dry run — verified by:
      `tests/tools/test_release_observation.py::test_late_link_between_existing_subjects_is_a_conflict_not_a_merge`
- [ ] Ambiguity: a key resolving to two distinct subjects creates its own subject, writes
      `identity_ambiguous` and increments the metric — verified by:
      `tests/tools/test_release_observation.py::test_ambiguous_identity_stays_separate`
- [ ] Same-source conflict guard: a candidate never merges into a release carrying a different
      non-provisional native id for the candidate's own source — verified by:
      `tests/tools/test_release_observation.py::test_never_merges_two_native_ids_of_one_source`
- [ ] `fold_title_key()` is the single fold used by `_normalized_title` and
      `_release_title_variant_key`; PR #59's parametrized variants and negative cases pass
      against it unchanged — verified by:
      `tests/store/test_catalog_records.py::test_title_key_folds_typographic_punctuation` and
      `::test_title_key_keeps_real_differences` (moved from `tests/tools/test_release_discovery.py`)
- [ ] Request budget: a run with N new release groups makes exactly N harvest requests through
      the existing `_PacedSource`; a run that hits the deadline leaves `link_harvest_pending`
      set and the next run harvests it — verified by:
      `tests/tools/test_refresh.py::test_link_harvest_is_bounded_and_resumable`
- [ ] `music_status.identity.conflicts` reports open conflicts; `_TOOL_SCHEMAS`, `docs/mcp.md`,
      skill and contract test updated; `docs/limits.md` request table updated; full gate green.

### Issue D: `data inbox duplicates` and CAS `unmerge` (absorbs #57 AC 3; replaces #59's `dedupe-inbox`)

- [ ] `data inbox duplicates` is dry-run by default, makes no network request, lists cross-
      subject pairs with tier, titles and both inbox states plus open conflicts, and writes
      nothing — verified by: `tests/runtimes/test_cli_data.py::test_inbox_duplicates_dry_run_writes_nothing`
- [ ] `--merge --yes` re-points the later release's subject, collapses inbox entries
      most-decided-wins with the head-signal rule, writes `subject_merges` and both snapshot
      roles with `winner_updated_at_after`, closes the conflict, promotes involved provisional
      references to `USER_CONFIRMED`; a second run lists nothing — verified by:
      `tests/runtimes/test_cli_data.py::test_inbox_duplicates_merge_is_idempotent_and_audited`
- [ ] `data inbox unmerge <merge_id> --yes` restores winner-before state, head signal and
      timestamps and re-points releases when the winner's `updated_at` still equals
      `winner_updated_at_after`; when the user changed the winner after the merge, it skips
      that winner, leaves the newer decision intact and prints the reason — verified by:
      `tests/runtimes/test_cli_data.py::test_inbox_unmerge_is_compare_and_swap`
- [ ] `dedupe-inbox` is removed from `_USAGE`, `docs/operations.md` and tests with a CHANGELOG
      note; `tests/docs/test_public_contract.py` passes.

### Issue E: refresh does not block MCP (absorbs #57 AC 4)

- [ ] `refresh_music` awaits `asyncio.to_thread`; with a fake source that blocks 2 s per
      artist outside any transaction, `list_inbox`, `explain_inbox_item` and `music_status`
      each complete in under 500 ms while the refresh runs in the same server process —
      verified by: `tests/mcp/test_catalog_server.py::test_read_tools_respond_during_refresh`
- [ ] Under the same conditions `update_inbox_item` completes in under 1 s — verified by:
      `tests/mcp/test_catalog_server.py::test_write_tool_completes_during_refresh`
- [ ] The refresh worker uses its own `Catalog`; the server connection is never used from the
      worker thread (thread-identity assertion in a test double) — verified by:
      `tests/runtimes/test_mcp_stdio.py::test_refresh_runs_on_a_worker_connection`
- [ ] No refresh transaction is open across a provider request (fake source asserts
      `connection.in_transaction is False` on every call) and a 100-release per-artist
      transaction commits in under 200 ms — verified by:
      `tests/tools/test_refresh.py::test_no_transaction_spans_a_source_call` and
      `::test_per_artist_transaction_is_short`
- [ ] `music_status.refresh.running` is `true` during a run and `false` after; `_TOOL_SCHEMAS`,
      `docs/mcp.md`, skill and contract test updated — verified by:
      `tests/mcp/test_catalog_server.py::test_status_reports_running_refresh` and
      `tests/docs/test_public_contract.py`
- [ ] A second `refresh_music` during a run returns `already_running` with `retry_after`
      (existing behavior) — verified by the existing test plus a threaded variant.

## 6. Out of scope, future PRs, deferred

**Out of scope for these PRs**

- Re-surfacing a `dismissed` item as `unread` when its release content changes (product
  decision; `state` stays untouched on update).
- User confirmation of release identity through MCP (`update_watchlist`-style `source_ids`
  for releases); the CLI merge is the user override for now.
- Merging *artists* across sources; artist identity mapping is #41's and unchanged.
- Rewriting historical `signals.material_version` values.

**Future PRs**

- Tier 3 barcode matching (`release_identifiers`, Deezer `GET /album/<id>` `upc`, MusicBrainz
  release `barcode` from the tier-2 harvest, Spotify `GET /albums/<id>` `external_ids.upc`).
  Trigger: `release_identity_ambiguous` or `release_identity_conflict` non-zero on the owner's
  library over two weeks after Issue C.
- Auto-merge of a late link when two independent tiers corroborate it (link plus title key),
  once the conflict queue shows the false-positive rate is acceptable.

**Deferred decisions**

- Whether `release_discoveries` should become per `(release, source)` rows; unnecessary once
  identity lookups move to `record_sources`.

## 7. Risks

- **Migration collapse picks the wrong winner.** Mitigated by full snapshots, the pre-upgrade
  backup instruction, CAS `unmerge`, and the synthetic fixture mirroring the observed patterns.
- **Editor mislinks in MusicBrainz.** Mitigated by provisional links, corroboration against
  the linked provider's own report, the same-source guard and the exactly-one rule; the
  residual failure mode is one extra visible item plus a conflict entry, never a hidden
  release.
- **Second in-process connection.** New for this codebase. Mitigated by WAL (already on),
  per-thread `Catalog` instances, short transactions bounded by test, and the lock file; the
  Issue E tests exercise the concurrent path with a slow fake source.

## 8. Prior art

- **MusicBrainz** separates *release group* (the album as a work), *release* (an edition:
  country, label, barcode, medium) and *recording* (ISRC lives here)
  ([Release Group](https://musicbrainz.org/doc/Release_Group)). Streaming and purchase links
  are release-level url-relationships (`free streaming`, `streaming`, `purchase for download`,
  [release-url relationships](https://musicbrainz.org/relationships/release-url)); release
  groups carry database and review links only. Verified live on 2026-09-26:
  `GET /ws/2/release?release-group=<rgid>&inc=url-rels` (Spotify and Deezer album links),
  `GET /ws/2/release-group/<rgid>?inc=url-rels` (no streaming rels),
  `GET /ws/2/url?resource=<deezer album url>&inc=release-rels` (resolves to the release), and
  `release?query=barcode:<upc>` (resolves to release and release group)
  ([API](https://musicbrainz.org/doc/MusicBrainz_API),
  [Search](https://musicbrainz.org/doc/MusicBrainz_API/Search)). This is why the design keys
  the subject to a release group's release row but harvests links and barcodes from its
  releases.
- **beets** tags `mb_albumid` (release) and `mb_releasegroupid` (release group) on import and
  its `duplicates` plugin keys albums on `mb_albumid` by default
  ([tagger guide](https://beets.readthedocs.io/en/stable/guides/tagger.html),
  [duplicates plugin](https://beets.readthedocs.io/en/stable/plugins/duplicates.html)).
- **Lidarr** models an Album as a MusicBrainz release group with releases as editions and keys
  it by `ForeignAlbumId` (the release-group MBID), which is what stops the same album being
  added twice ([Servarr wiki](https://wiki.servarr.com/lidarr/concepts),
  [Lidarr wiki: Album](https://github.com/lidarr/Lidarr/wiki/Album)). Same shape as
  `inbox_subjects`.
- **MetaBrainz canonical data / ListenBrainz** collapse every release MBID onto one canonical
  release via `canonical_release_redirect` so editions of one album resolve to one identity
  ([Canonical MusicBrainz data](https://musicbrainz.org/doc/Canonical_MusicBrainz_data),
  [announcement](https://blog.metabrainz.org/2023/06/12/new-dataset-musicbrainz-canonical-metadata/)).
  A subject merge is the local, reversible form of that redirect.
- **Spotify Web API**: `external_ids.upc` is on the full Album object
  ([Get Album](https://developer.spotify.com/documentation/web-api/reference/get-an-album)),
  `external_ids.isrc` on Track
  ([Get Track](https://developer.spotify.com/documentation/web-api/reference/get-track)), and
  neither on `GET /artists/{id}/albums` items
  ([Get Artist's Albums](https://developer.spotify.com/documentation/web-api/reference/get-an-artists-albums)).
- **Deezer API**: `upc` on `GET /album/{id}` only (live probe 2026-09-26).

## 9. Research notes

Gathered by a scoped researcher pass plus live read-only probes on 2026-09-26; sources are
inline in section 8. Stated gaps: Lidarr's `ForeignAlbumId` uniqueness was read from docs, not
C# source; beets' `find_duplicates` from docs, not source; Deezer track-level ISRC and Apple
Music identifiers were not examined. None of these gaps changes a decision here: the design
relies only on facts verified live (MusicBrainz release url-rels and barcodes, Deezer `upc`
placement) and on the shape shared by all three tools (album identity = release group).

## Review trail

**Round:** one cross-family adversarial round per `skills/adversarial-design`.
**A (author):** designer, Claude family. **B (reviewer):** codex-dev, Codex family
(`gpt-5.6-terra`), read-only, 2026-09-26. **Verdict: MAJOR_CONCERNS.** B's report reached the
coordinator; the findings below are recorded from the coordinator's relay, so wording is
paraphrased. Both positions are preserved here.

**B's counter-shape (four bullets):** (1) key the inbox on a durable identity cluster
(`inbox_subject`) that `releases` map onto many-to-one, with `UNIQUE` inbox-per-subject,
because `UNIQUE` per `releases` row still admits two rows for one real-world release (tier-4
miss, later tier-2 arrival); (2) treat MusicBrainz url-rels as provisional until corroborated
or user-confirmed and keep a conflict state, because a valid-but-wrong link fills an empty
provider slot and tier 1 then ratifies it; (3) define the async boundary explicitly and bound
writer hold time, because `refresh_music` is a synchronous callback under `_safe_call` and a
contended `BEGIN IMMEDIATE` waits up to the 5 s `busy_timeout`, so a blanket sub-500 ms AC was
not achievable; (4) make unmerge compare-and-swap over pre-merge snapshots so recovery never
overwrites a later decision, and say what is recoverable. Minor: define the winner's head
signal; make content conflicts deterministic and recorded.
**Trade named:** more schema (subjects, snapshots, a merge log) and a slower path to a
duplicate-free inbox for late links (visible conflict, user merge) in exchange for an
invariant that actually matches the real-world release and a recovery that cannot lose a
decision.

**A's engagement:**

1. *Subject cluster* — **conceded.** Section 2 and 3.1 now introduce `inbox_subjects`,
   `releases.subject_local_id`, `UNIQUE (kind, subject_local_id)`; release merges become
   reversible subject re-points; release `local_id`s stay stable for MCP callers. Issue A AC
   amended (Goal-mapped: the Goal is "never a second item for a release the catalog knows",
   and a per-row key did not deliver that).
2. *Provisional links* — **conceded.** `IdentityConfidence.PROVISIONAL`, the corroboration
   rule on a provisional tier-1 hit, detach-on-contradiction, the "late links never auto-merge
   subjects" rule and the `identity_conflict` state are in section 3.4; Issue C gains the
   valid-but-wrong url-rel AC and the late-link AC. Invariant-mapped: the repository's stated
   rule that a false merge is worse than a duplicate.
3. *Concurrency* — **conceded with a precise boundary.** Section 3.7 names the
   `asyncio.to_thread` boundary and `_safe_call_async`, separates reads (autocommit, WAL, never
   wait; sub-500 ms AC) from writes (one short `BEGIN IMMEDIATE`, bounded by the refresh's
   current transaction; under 1 s AC), and bounds writer hold time by construction with two
   tests (no transaction across a provider call; 100-release transaction under 200 ms).
4. *CAS unmerge* — **conceded.** `inbox_entry_merges` is replaced by `inbox_entry_snapshots`
   (winner-before and loser rows, `winner_updated_at_after` as the CAS token) plus
   `subject_merges`; section 3.6 defines recoverable vs not.
5. *Minors* — **conceded.** Head signal = greatest `observed_at` (tie: `local_id`), in the
   migration SQL and the CLI merge. Content precedence is `release_sources` order with
   precision override for dates, and each overruled value writes a `content_conflict:<field>`
   observation (section 3.3.1; Issue B AC).

**Adjacent observations (not AC):** none of B's findings fell outside the Goal or an existing
invariant, so none were parked.

**Result:** shape locked as revised; no escalation needed. The draft PR (#60) carries this
revision; principal review follows.
