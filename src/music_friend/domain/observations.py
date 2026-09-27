"""Observation types for the single release write path (issue #62)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal

from music_friend.domain.models import RecordId, Release, SourceReference

OutcomeKind = Literal[
    "created", "updated", "unchanged", "provenance_attached", "ambiguous", "conflict"
]
_OUTCOME_KINDS = frozenset(
    {"created", "updated", "unchanged", "provenance_attached", "ambiguous", "conflict"}
)


class IdentityMethod(str, Enum):
    """How a release identity was resolved."""

    NATIVE_ID = "native_id"
    EXTERNAL_LINK = "external_link"
    BARCODE = "barcode"
    TITLE_KEY = "title_key"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ReleaseObservation:
    """One observed release from one source, ready for the single write path."""

    release: Release
    source: str
    native_id: str
    monitored_artist_local_id: RecordId
    external_links: tuple[SourceReference, ...] = ()
    barcodes: tuple[str, ...] = ()
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.release, Release):
            raise ValueError("release must be a Release")
        for name in ("source", "native_id", "monitored_artist_local_id"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.external_links, tuple) or not all(
            isinstance(link, SourceReference) for link in self.external_links
        ):
            raise ValueError("external_links must be a tuple of source references")
        if not isinstance(self.barcodes, tuple) or not all(
            type(barcode) is str and barcode for barcode in self.barcodes
        ):
            raise ValueError("barcodes must be a tuple of non-empty strings")
        if self.observed_at is not None and (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ObservationOutcome:
    """Result of recording one release observation."""

    kind: OutcomeKind
    release_local_id: RecordId
    subject_local_id: RecordId
    inbox_local_id: RecordId | None
    method: IdentityMethod | None = None
    signal_created: bool = False
    #: ``identity_conflict`` observations this recording wrote (issue #63).
    identity_conflicts: int = 0

    def __post_init__(self) -> None:
        if self.kind not in _OUTCOME_KINDS:
            raise ValueError("kind must be a known observation outcome")
        if type(self.identity_conflicts) is not int or self.identity_conflicts < 0:
            raise ValueError("identity_conflicts must be a non-negative integer")
