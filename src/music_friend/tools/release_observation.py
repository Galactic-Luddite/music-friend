"""The single write path for release signals and inbox entries (issue #62).

``record_release_observation`` is the only code in ``src/`` that writes a release signal or
inserts/repoints a release inbox entry. Calling it twice with the same observation is a no-op
the second time: the signal identity is a digest of the release's *content* only, so attaching
a second source, re-observing a release later, or running the repair pass never mints a second
signal or a second inbox item.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256

from music_friend.domain import (
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    Observation,
    Release,
    ReleaseDatePrecision,
    Signal,
    SignalKind,
    SourceReference,
)
from music_friend.domain.observations import (
    IdentityMethod,
    ObservationOutcome,
    OutcomeKind,
    ReleaseObservation,
)
from music_friend.store import Catalog

_CONTENT_VERSION = 3
_PRECISION_RANK = {
    ReleaseDatePrecision.DAY: 0,
    ReleaseDatePrecision.MONTH: 1,
    ReleaseDatePrecision.YEAR: 2,
}


@dataclass(frozen=True, slots=True)
class ContentConflict:
    """One overruled field value, recorded against the losing source's reference."""

    field: str
    loser: SourceReference


def record_release_observation(
    catalog: Catalog,
    observation: ReleaseObservation,
    *,
    release_sources: tuple[str, ...],
) -> ObservationOutcome:
    """Record one observed release through the single write path; idempotent on repeat."""
    if not isinstance(catalog, Catalog):
        raise ValueError("catalog must be a Catalog")
    if not isinstance(observation, ReleaseObservation):
        raise ValueError("observation must be a ReleaseObservation")
    if (
        not isinstance(release_sources, tuple)
        or not release_sources
        or not all(type(source) is str and source for source in release_sources)
    ):
        raise ValueError("release_sources must be a non-empty tuple of source names")
    reference = _observation_reference(observation)
    observed_at = observation.observed_at or observation.release.observed_at
    with catalog.transaction():
        method, matches = _resolve_release_identity(catalog, observation)
        attached = False
        if len(matches) > 1:
            release_local_id = observation.release.local_id
            if release_local_id in matches:
                raise ValueError("an ambiguous observation cannot reuse a matched release id")
            catalog.put_release(replace(observation.release, subject_local_id=None))
            fact = (
                "identity_conflict"
                if method is IdentityMethod.EXTERNAL_LINK
                else ("identity_ambiguous")
            )
            _put_fact(catalog, release_local_id, reference, fact, observed_at)
        elif not matches:
            release_local_id = observation.release.local_id
            # Add external_links to the release even when there's no match
            # so they're available for future tier-1 lookups
            known = {(item.source, item.native_id) for item in observation.release.source_refs}
            new_references = tuple(
                item for item in observation.external_links
                if (item.source, item.native_id) not in known
            )
            release_with_links = replace(
                observation.release,
                source_refs=observation.release.source_refs + new_references,
                subject_local_id=None,
            )
            catalog.put_release(release_with_links)
        else:
            release_local_id = matches[0]
            attached = _attach_and_merge(
                catalog,
                release_local_id,
                observation,
                reference,
                release_sources,
                observed_at,
            )
        stored = catalog.get_release(release_local_id)
        if stored is None:
            raise ValueError("recorded release disappeared")
        subject_local_id = stored.subject_local_id or stored.local_id
        content_version = content_version_of(stored)
        signal = catalog.find_signal_for_record(
            SignalKind.RELEASE, release_local_id, content_version
        )
        entry = catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, subject_local_id)
        signal_created = False
        if signal is None:
            signal = _new_signal(
                catalog,
                stored,
                observation,
                reference,
                content_version,
                ExplanationReasonKind.NEW_RELEASE
                if entry is None
                else ExplanationReasonKind.UPDATED_RELEASE,
                observed_at,
            )
            catalog.put_signal(signal)
            signal_created = True
        if entry is None:
            entry = catalog.upsert_inbox_entry(
                SignalKind.RELEASE,
                subject_local_id,
                signal.local_id,
                local_id=inbox_id(signal.local_id),
                at=observed_at,
            )
            kind: OutcomeKind = "created"
        elif entry.latest_signal_local_id != signal.local_id:
            entry = catalog.upsert_inbox_entry(
                SignalKind.RELEASE,
                subject_local_id,
                signal.local_id,
                local_id=entry.local_id,
                at=max(observed_at, entry.created_at),
            )
            kind = "updated"
        else:
            kind = "provenance_attached" if attached else "unchanged"
    if len(matches) > 1:
        kind = "conflict" if method is IdentityMethod.EXTERNAL_LINK else "ambiguous"
    return ObservationOutcome(
        kind=kind,
        release_local_id=release_local_id,
        subject_local_id=subject_local_id,
        inbox_local_id=entry.local_id,
        method=method,
        signal_created=signal_created,
    )


def content_version_of(release: Release) -> str:
    """Digest only a release's content; provenance never changes it.

    ``source_refs``, ``canonical_url``, ``observed_at``, ``subject_local_id`` and the
    explanation are excluded by construction, so attaching a second source or re-observing
    a release later hashes equal, while a title or date change hashes different.
    """
    if not isinstance(release, Release):
        raise ValueError("release must be a Release")
    value = {
        "version": _CONTENT_VERSION,
        "title": release.title,
        "release_type": release.release_type,
        "release_date": release.release_date.isoformat(),
        "date_precision": release.date_precision.value,
        "artist_refs": list(release.artist_refs),
    }
    return f"material:{digest(value)}"


def merge_release_content(
    stored: Release,
    incoming: Release,
    incoming_reference: SourceReference,
    release_sources: tuple[str, ...],
) -> tuple[Release, tuple[ContentConflict, ...]]:
    """Deterministically merge ``incoming`` content onto ``stored``.

    ``title`` and ``release_type`` follow the configured ``release_sources`` order: the
    earliest-ranked source that has reported the release wins regardless of arrival order.
    ``release_date``/``date_precision`` take the more precise value (DAY over MONTH over
    YEAR), then ``release_sources`` order. ``artist_refs`` is the first-seen-order union.
    Every overruled differing value is returned as a conflict against the losing reference.
    A source that is the only one reporting the release always wins (it is correcting itself).
    """
    others = tuple(
        reference
        for reference in stored.source_refs
        if reference.source != incoming_reference.source
    )
    if not others:
        merged = replace(
            stored,
            title=incoming.title,
            release_type=incoming.release_type,
            release_date=incoming.release_date,
            date_precision=incoming.date_precision,
            artist_refs=_union(stored.artist_refs, incoming.artist_refs),
        )
        return merged, ()
    best_other = min(others, key=lambda reference: _rank(reference.source, release_sources))
    incoming_outranks = _rank(incoming_reference.source, release_sources) < _rank(
        best_other.source, release_sources
    )
    conflicts: list[ContentConflict] = []
    title = stored.title
    release_type = stored.release_type
    if incoming.title != stored.title:
        title = incoming.title if incoming_outranks else stored.title
        conflicts.append(
            ContentConflict("title", best_other if incoming_outranks else incoming_reference)
        )
    if incoming.release_type != stored.release_type:
        release_type = incoming.release_type if incoming_outranks else stored.release_type
        conflicts.append(
            ContentConflict("release_type", best_other if incoming_outranks else incoming_reference)
        )
    release_date = stored.release_date
    date_precision = stored.date_precision
    if (incoming.release_date, incoming.date_precision) != (
        stored.release_date,
        stored.date_precision,
    ):
        incoming_rank = _PRECISION_RANK[incoming.date_precision]
        stored_rank = _PRECISION_RANK[stored.date_precision]
        incoming_date_wins = incoming_rank < stored_rank or (
            incoming_rank == stored_rank and incoming_outranks
        )
        if incoming_date_wins:
            release_date, date_precision = incoming.release_date, incoming.date_precision
        conflicts.append(
            ContentConflict(
                "release_date", best_other if incoming_date_wins else incoming_reference
            )
        )
    merged = replace(
        stored,
        title=title,
        release_type=release_type,
        release_date=release_date,
        date_precision=date_precision,
        artist_refs=_union(stored.artist_refs, incoming.artist_refs),
    )
    return merged, tuple(conflicts)


def fingerprint(provider: str, kind: SignalKind, provider_native_id: str) -> str:
    return f"signal:{digest((provider, kind.value, provider_native_id))}"


def signal_id(
    provider: str, kind: SignalKind, provider_native_id: str, material_version: str
) -> str:
    return f"mf:{digest((provider, kind.value, provider_native_id, material_version))}"


def inbox_id(signal_local_id: str) -> str:
    return f"inbox:{digest(signal_local_id)}"


def digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _observation_reference(observation: ReleaseObservation) -> SourceReference:
    matching = tuple(
        reference
        for reference in observation.release.source_refs
        if reference.source == observation.source and reference.native_id == observation.native_id
    )
    if len(matching) != 1:
        raise ValueError("observation release must carry exactly its own source reference")
    return matching[0]


def _resolve_release_identity(
    catalog: Catalog, observation: ReleaseObservation
) -> tuple[IdentityMethod, tuple[str, ...]]:
    """Resolve release identity through the ladder: tier 1 (native), tier 2 (external links),
    tier 4 (title key). Returns every matching release; more than one is ambiguous or a conflict.

    Tier 1 (native id): a candidate whose (source, native_id) is already attached as a
    non-provisional reference merges without consulting lower tiers.

    Tier 2 (external links): each link in observation.external_links is looked up via
    tier 1 (by source and native_id). Matches are checked for corroboration: if a link is
    provisional and the release it targets differs from the one tier 1 would pick, it's a
    late link (conflict, not merge). All tier-2 matches must resolve to the same release
    (exactly-one-or-nothing).

    Tier 4 (title key): conservative title matching with the existing find_release_discovery_variant.
    """
    from music_friend.domain import IdentityConfidence

    # Tier 1: native_id (only non-provisional references, issue #63)
    native = catalog.find_releases_by_source_reference(observation.source, observation.native_id)
    # Filter to only non-provisional references
    native_non_provisional: list[str] = []
    for release_local_id in native:
        release = catalog.get_release(release_local_id)
        if release is not None:
            for ref in release.source_refs:
                if (ref.source == observation.source and ref.native_id == observation.native_id
                    and ref.confidence != IdentityConfidence.PROVISIONAL):
                    native_non_provisional.append(release_local_id)
                    break
    if native_non_provisional:
        # Same-source guard: never merge if target already has a different non-provisional native_id
        # from the same source
        filtered: list[str] = []
        for candidate_id in native_non_provisional:
            candidate = catalog.get_release(candidate_id)
            if candidate is not None:
                # Check if candidate has a different non-provisional reference from observation.source
                has_other_native_id = False
                for ref in candidate.source_refs:
                    if (ref.source == observation.source
                        and ref.native_id != observation.native_id
                        and ref.confidence != IdentityConfidence.PROVISIONAL):
                        has_other_native_id = True
                        break
                if not has_other_native_id:
                    filtered.append(candidate_id)
        if filtered:
            return IdentityMethod.NATIVE_ID, tuple(filtered)

    # Tier 2: external_links (provisional by default)
    # Each link must resolve to the same release (exactly-one-or-nothing)
    linked_releases: list[str] = []
    for link in observation.external_links:
        link_matches = catalog.find_releases_by_source_reference(link.source, link.native_id)
        for release_local_id in link_matches:
            release = catalog.get_release(release_local_id)
            if release is not None:
                # Check if this release has the link reference
                for ref in release.source_refs:
                    if ref.source == link.source and ref.native_id == link.native_id:
                        # For provisional links, check corroboration:
                        # if the link is marked provisional, it still requires corroboration
                        # (it must match by content or be explicitly confirmed)
                        if link.confidence == IdentityConfidence.PROVISIONAL:
                            # Check if content matches (title, date)
                            if (ref.confidence == IdentityConfidence.PROVISIONAL
                                or (observation.release.title == release.title
                                    and observation.release.release_date == release.release_date)):
                                if release_local_id not in linked_releases:
                                    linked_releases.append(release_local_id)
                        else:
                            # Non-provisional link (shouldn't normally happen in external_links)
                            if release_local_id not in linked_releases:
                                linked_releases.append(release_local_id)
                        break

    if linked_releases:
        # All tier-2 matches must resolve to the same release
        return IdentityMethod.EXTERNAL_LINK, tuple(linked_releases)

    # Tier 4: title_key (existing discovery logic, deferred to release_discovery layer)
    return IdentityMethod.NONE, ()


def _attach_and_merge(
    catalog: Catalog,
    release_local_id: str,
    observation: ReleaseObservation,
    reference: SourceReference,
    release_sources: tuple[str, ...],
    observed_at: datetime,
) -> bool:
    stored = catalog.get_release(release_local_id)
    if stored is None:
        raise ValueError("matched release disappeared")
    known = {(item.source, item.native_id) for item in stored.source_refs}
    new_references = tuple(
        item
        for item in (reference, *observation.external_links)
        if (item.source, item.native_id) not in known
    )
    with_references = replace(stored, source_refs=stored.source_refs + new_references)
    merged, conflicts = merge_release_content(
        with_references, observation.release, reference, release_sources
    )
    if new_references or content_version_of(merged) != content_version_of(stored):
        catalog.put_release(merged)
    for item in new_references:
        _put_fact(catalog, release_local_id, item, "source_attached", observed_at)
    for conflict in conflicts:
        _put_fact(
            catalog,
            release_local_id,
            conflict.loser,
            f"content_conflict:{conflict.field}",
            observed_at,
        )
    return bool(new_references)


def _put_fact(
    catalog: Catalog,
    release_local_id: str,
    reference: SourceReference,
    fact_name: str,
    observed_at: datetime,
) -> None:
    catalog.put_observation(
        Observation(
            f"observation:{digest((release_local_id, reference.source, reference.native_id, fact_name))}",
            reference.source,
            reference.native_id,
            "release",
            release_local_id,
            fact_name,
            observed_at,
        )
    )


def _new_signal(
    catalog: Catalog,
    release: Release,
    observation: ReleaseObservation,
    reference: SourceReference,
    content_version: str,
    reason: ExplanationReasonKind,
    observed_at: datetime,
) -> Signal:
    artist = catalog.get_artist(observation.monitored_artist_local_id)
    if artist is None:
        raise ValueError("observed release artist does not exist")
    explanation = Explanation(
        (
            ExplanationReason(ExplanationReasonKind.MONITORED_ARTIST, artist.display_name),
            ExplanationReason(reason, release.title),
        )
    )
    return Signal(
        signal_id(reference.source, SignalKind.RELEASE, reference.native_id, content_version),
        SignalKind.RELEASE,
        release.local_id,
        reference.source,
        reference.native_id,
        fingerprint(reference.source, SignalKind.RELEASE, reference.native_id),
        content_version,
        explanation,
        observed_at,
    )


def _rank(source: str, release_sources: tuple[str, ...]) -> int:
    return release_sources.index(source) if source in release_sources else len(release_sources)


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for item in (*first, *second):
        if item not in merged:
            merged.append(item)
    return tuple(merged)


__all__ = [
    "ContentConflict",
    "content_version_of",
    "merge_release_content",
    "record_release_observation",
]
