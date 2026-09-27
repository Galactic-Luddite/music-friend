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
from typing import Literal

from music_friend.domain import (
    Explanation,
    ExplanationReason,
    ExplanationReasonKind,
    IdentityConfidence,
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
from music_friend.store.catalog import release_title_keys_compatible

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
    """Record one observed release through the single write path; idempotent on repeat.

    Identity is resolved by the ladder (issue #63, design section 3.4): tier 1 native id,
    with a provisional hit corroborated by the tier-4 key, then tier 2 external links. Every
    tier accepts exactly one subject or none; late links are recorded, never merged.
    """
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
    if reference.confidence is IdentityConfidence.PROVISIONAL:
        raise ValueError("an observation's own source reference cannot be provisional")
    links = _external_links(observation)
    observed_at = observation.observed_at or observation.release.observed_at
    with catalog.transaction():
        resolution = _resolve_release_identity(catalog, observation, links)
        own_local_id = observation.release.local_id
        self_stored = catalog.get_release(own_local_id)
        conflicts = 0
        attached = False
        if resolution.detach_from:
            for holder in resolution.detach_from:
                _detach_reference(catalog, holder, reference)
        if resolution.target is None:
            release_local_id = own_local_id
            if self_stored is None:
                catalog.put_release(replace(observation.release, subject_local_id=None))
            if resolution.kind == "ambiguous":
                _put_fact(catalog, own_local_id, reference, "identity_ambiguous", observed_at)
            elif resolution.kind == "conflict":
                _put_fact(catalog, own_local_id, reference, "identity_conflict", observed_at)
                conflicts += 1
            link_holder = own_local_id
        elif self_stored is not None and resolution.target != own_local_id:
            # A release discovery created this release moments ago: map it onto the matched
            # subject instead of copying its reference, so each reference keeps one holder.
            release_local_id = own_local_id
            _promote(catalog, resolution.target, resolution.promote)
            attached = resolution.promote is not None
            target_subject = _subject_of(catalog, resolution.target)
            if target_subject != (self_stored.subject_local_id or own_local_id):
                assert target_subject is not None
                catalog.join_new_release_to_subject(own_local_id, target_subject)
                attached = True
            link_holder = own_local_id
        else:
            release_local_id = resolution.target
            _promote(catalog, resolution.target, resolution.promote)
            attached = (
                _attach_and_merge(
                    catalog,
                    release_local_id,
                    observation,
                    reference,
                    release_sources,
                    observed_at,
                )
                or resolution.promote is not None
            )
            link_holder = release_local_id
        attached = _attach_links(catalog, link_holder, resolution.attach_links, observed_at) or (
            attached
        )
        for other_release_local_id in resolution.late_links:
            _record_late_link(catalog, link_holder, reference, other_release_local_id, observed_at)
            conflicts += 1
        stored = catalog.get_release(release_local_id)
        if stored is None:
            raise ValueError("recorded release disappeared")
        subject_local_id = stored.subject_local_id or stored.local_id
        content_version = content_version_of(stored)
        entry = catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, subject_local_id)
        signal = catalog.find_signal_for_record(
            SignalKind.RELEASE, release_local_id, content_version
        )
        # A release that joined an existing item and never had a signal of its own is another
        # source's report of that item: provenance only, never a second or updated item.
        joined_item = entry is not None and not catalog.record_has_signal(
            SignalKind.RELEASE, release_local_id
        )
        signal_created = False
        if signal is None and not joined_item:
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
            assert signal is not None
            entry = catalog.upsert_inbox_entry(
                SignalKind.RELEASE,
                subject_local_id,
                signal.local_id,
                local_id=inbox_id(signal.local_id),
                at=observed_at,
            )
            kind: OutcomeKind = "created"
        elif signal is not None and entry.latest_signal_local_id != signal.local_id:
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
    if resolution.kind in ("ambiguous", "conflict"):
        kind = resolution.kind
    return ObservationOutcome(
        kind=kind,
        release_local_id=release_local_id,
        subject_local_id=subject_local_id,
        inbox_local_id=entry.local_id,
        method=resolution.method,
        signal_created=signal_created,
        identity_conflicts=conflicts,
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


@dataclass(frozen=True, slots=True)
class _Holder:
    release_local_id: str
    subject_local_id: str
    provisional: bool


@dataclass(frozen=True, slots=True)
class _Resolution:
    """What the ladder decided for one observation; applied by the write path."""

    kind: Literal["match", "none", "ambiguous", "conflict"]
    method: IdentityMethod
    target: str | None = None
    promote: SourceReference | None = None
    detach_from: tuple[str, ...] = ()
    attach_links: tuple[SourceReference, ...] = ()
    late_links: tuple[str, ...] = ()


def _external_links(observation: ReleaseObservation) -> tuple[SourceReference, ...]:
    """Links another source asserted, always stored ``PROVISIONAL`` until corroborated.

    A link naming the observation's own source is dropped: a source's identity for its own
    releases comes only from its own report (same-source conflict guard).
    """
    seen: set[tuple[str, str]] = set()
    links: list[SourceReference] = []
    for link in observation.external_links:
        key = (link.source, link.native_id)
        if link.source == observation.source or key in seen:
            continue
        seen.add(key)
        links.append(replace(link, confidence=IdentityConfidence.PROVISIONAL))
    return tuple(links)


def _holders(catalog: Catalog, source: str, native_id: str, exclude: str) -> tuple[_Holder, ...]:
    return tuple(
        _Holder(release_local_id, subject_local_id, provisional)
        for release_local_id, subject_local_id, provisional in (
            catalog.find_release_reference_holders(source, native_id)
        )
        if release_local_id != exclude
    )


def _subjects(holders: tuple[_Holder, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(holder.subject_local_id for holder in holders))


def _subject_carries_other_native_id(
    catalog: Catalog, subject_local_id: str, source: str, native_id: str
) -> bool:
    """Same-source conflict guard: does the subject already hold another id of ``source``?"""
    for release_local_id in catalog.list_release_local_ids_for_subject(subject_local_id):
        release = _require_release(catalog, release_local_id)
        for item in release.source_refs:
            if (
                item.source == source
                and item.native_id != native_id
                and item.confidence is not IdentityConfidence.PROVISIONAL
            ):
                return True
    return False


def _corroborates(catalog: Catalog, candidate: Release, release_local_id: str) -> bool:
    """A provisional hit counts only when the linked report agrees on the tier-4 key."""
    stored = _require_release(catalog, release_local_id)
    return bool(set(candidate.artist_refs) & set(stored.artist_refs)) and (
        release_title_keys_compatible(
            candidate.title,
            candidate.release_date,
            candidate.date_precision,
            stored.title,
            stored.release_date,
            stored.date_precision,
        )
    )


def _require_release(catalog: Catalog, release_local_id: str) -> Release:
    stored = catalog.get_release(release_local_id)
    if stored is None:
        raise ValueError("a release the ladder resolved has disappeared")
    return stored


def _subject_of(catalog: Catalog, release_local_id: str) -> str | None:
    stored = catalog.get_release(release_local_id)
    return None if stored is None else (stored.subject_local_id or stored.local_id)


def _resolve_release_identity(
    catalog: Catalog,
    observation: ReleaseObservation,
    links: tuple[SourceReference, ...],
) -> _Resolution:
    """Resolve release identity through the ladder (issue #63, design section 3.4).

    Tier 1: the observation's own ``(source, native_id)``. A confirmed reference on exactly
    one subject merges; on two or more subjects it is ambiguous. A provisional reference
    (harvested by another source) merges only when this report corroborates it (tier-4 key:
    compatible title variant, same date or one day apart at day precision, a shared artist)
    and the subject holds no other id of this source; otherwise the link is detached and this
    report stays its own release, recorded as ``identity_conflict``.

    Tier 2: each external link, looked up among confirmed references. All links must name one
    subject, and that subject must hold no other id of this source.

    Tiers do not vote: the first tier that decides wins. A link naming a release in another
    subject than the decided one is a late link: recorded, never merged. A link naming no
    release fills the empty slot, stored ``PROVISIONAL``. Tier 4 (title key) runs in release
    discovery before an observation reaches this ladder.
    """
    own_local_id = observation.release.local_id
    self_subject = _subject_of(catalog, own_local_id)
    established = self_subject is not None and (
        catalog.get_inbox_entry_for_subject(SignalKind.RELEASE, self_subject) is not None
    )
    decided: _Resolution | None = None
    if established:
        decided = _Resolution("match", IdentityMethod.NATIVE_ID, own_local_id)
    else:
        own = _holders(catalog, observation.source, observation.native_id, own_local_id)
        confirmed = tuple(holder for holder in own if not holder.provisional)
        provisional = tuple(holder for holder in own if holder.provisional)
        if confirmed:
            subjects = _subjects(confirmed)
            if len(subjects) > 1:
                return _Resolution("ambiguous", IdentityMethod.NATIVE_ID)
            if not _subject_carries_other_native_id(
                catalog, subjects[0], observation.source, observation.native_id
            ):
                decided = _Resolution(
                    "match", IdentityMethod.NATIVE_ID, confirmed[0].release_local_id
                )
        elif provisional:
            if len(_subjects(provisional)) > 1:
                return _Resolution("ambiguous", IdentityMethod.NATIVE_ID)
            holder = provisional[0]
            if _corroborates(
                catalog, observation.release, holder.release_local_id
            ) and not _subject_carries_other_native_id(
                catalog, holder.subject_local_id, observation.source, observation.native_id
            ):
                decided = _Resolution(
                    "match",
                    IdentityMethod.NATIVE_ID,
                    holder.release_local_id,
                    promote=replace(
                        _observation_reference(observation),
                        confidence=IdentityConfidence.EXTERNAL_ID,
                    ),
                )
            else:
                return _Resolution(
                    "conflict",
                    IdentityMethod.EXTERNAL_LINK,
                    detach_from=tuple(item.release_local_id for item in provisional),
                )
    if decided is None:
        linked: list[_Holder] = []
        for link in links:
            linked.extend(
                holder
                for holder in _holders(catalog, link.source, link.native_id, own_local_id)
                if not holder.provisional
            )
        subjects = _subjects(tuple(linked))
        if len(subjects) > 1:
            return _Resolution("conflict", IdentityMethod.EXTERNAL_LINK)
        if subjects and not _subject_carries_other_native_id(
            catalog, subjects[0], observation.source, observation.native_id
        ):
            decided = _Resolution("match", IdentityMethod.EXTERNAL_LINK, linked[0].release_local_id)
    if decided is None:
        decided = _Resolution("none", IdentityMethod.NONE)
    decided_subject = (
        self_subject if decided.target is None else _subject_of(catalog, decided.target)
    )
    attach: list[SourceReference] = []
    late: list[str] = []
    for link in links:
        confirmed_link = tuple(
            holder
            for holder in _holders(catalog, link.source, link.native_id, own_local_id)
            if not holder.provisional
        )
        if not confirmed_link:
            attach.append(link)
            continue
        for holder in confirmed_link:
            if holder.subject_local_id != decided_subject and holder.release_local_id not in late:
                late.append(holder.release_local_id)
    return replace(decided, attach_links=tuple(attach), late_links=tuple(late))


def _promote(catalog: Catalog, release_local_id: str, promoted: SourceReference | None) -> None:
    """Corroborated: the provisional reference now counts as a confirmed external id."""
    if promoted is None:
        return
    stored = _require_release(catalog, release_local_id)
    catalog.put_release(
        replace(
            stored,
            source_refs=tuple(
                promoted
                if (item.source, item.native_id) == (promoted.source, promoted.native_id)
                else item
                for item in stored.source_refs
            ),
        )
    )


def _detach_reference(catalog: Catalog, release_local_id: str, reference: SourceReference) -> None:
    """Remove a provisional link its own provider contradicted (valid-but-wrong url-rel)."""
    stored = _require_release(catalog, release_local_id)
    remaining = tuple(
        item
        for item in stored.source_refs
        if (item.source, item.native_id) != (reference.source, reference.native_id)
        or item.confidence is not IdentityConfidence.PROVISIONAL
    )
    catalog.put_release(replace(stored, source_refs=remaining))


def _attach_links(
    catalog: Catalog,
    release_local_id: str,
    links: tuple[SourceReference, ...],
    observed_at: datetime,
) -> bool:
    """Fill empty slots with provisional links; a subject's own confirmed id always wins."""
    stored = _require_release(catalog, release_local_id)
    known = {(item.source, item.native_id) for item in stored.source_refs}
    subject_local_id = stored.subject_local_id or stored.local_id
    new_links = tuple(
        link
        for link in links
        if (link.source, link.native_id) not in known
        and not _subject_carries_other_native_id(
            catalog, subject_local_id, link.source, link.native_id
        )
    )
    if not new_links:
        return False
    catalog.put_release(replace(stored, source_refs=stored.source_refs + new_links))
    for link in new_links:
        _put_fact(catalog, release_local_id, link, "link_provisional", observed_at)
    return True


def _record_late_link(
    catalog: Catalog,
    release_local_id: str,
    reference: SourceReference,
    other_release_local_id: str,
    observed_at: datetime,
) -> None:
    """A link between two existing subjects: an open conflict for the user, never a merge."""
    observation_local_id = _put_fact(
        catalog,
        release_local_id,
        reference,
        "identity_conflict",
        observed_at,
        discriminator=other_release_local_id,
    )
    catalog.open_release_identity_conflict(
        observation_local_id, release_local_id, other_release_local_id, observed_at
    )


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
        item for item in (reference,) if (item.source, item.native_id) not in known
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
    *,
    discriminator: str | None = None,
) -> str:
    key: tuple[str, ...] = (release_local_id, reference.source, reference.native_id, fact_name)
    if discriminator is not None:
        key = (*key, discriminator)
    local_id = f"observation:{digest(key)}"
    catalog.put_observation(
        Observation(
            local_id,
            reference.source,
            reference.native_id,
            "release",
            release_local_id,
            fact_name,
            observed_at,
        )
    )
    return local_id


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
