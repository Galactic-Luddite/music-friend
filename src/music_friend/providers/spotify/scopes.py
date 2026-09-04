"""Closed Spotify read-scope to capability mapping."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from music_friend.providers import Capability, ProviderCapabilities

_SCOPES_BY_CAPABILITY: Mapping[Capability, frozenset[str]] = MappingProxyType(
    {
        Capability.HEALTH: frozenset({"user-read-private"}),
        Capability.SEARCH_ARTISTS: frozenset({"user-read-private"}),
        Capability.FOLLOWED_ARTISTS: frozenset({"user-follow-read"}),
        Capability.SAVED_ITEMS: frozenset({"user-library-read"}),
        Capability.TOP_ITEMS: frozenset({"user-top-read"}),
        Capability.TOP_ARTISTS: frozenset({"user-top-read"}),
        Capability.RECENT_RELEASES: frozenset(),
    }
)
_SUPPORTED_CAPABILITIES = frozenset(_SCOPES_BY_CAPABILITY)


def _scopes_for_capabilities(
    capabilities: set[Capability] | frozenset[Capability],
) -> frozenset[str]:
    if not isinstance(capabilities, (set, frozenset)) or not all(
        isinstance(capability, Capability) for capability in capabilities
    ):
        raise ValueError("capabilities must be a set of supported capabilities")
    if not capabilities.issubset(_SUPPORTED_CAPABILITIES):
        raise ValueError("capabilities must be a set of supported capabilities")
    return frozenset(
        scope for capability in capabilities for scope in _SCOPES_BY_CAPABILITY[capability]
    )


def _capabilities_for_scopes(
    granted_scopes: frozenset[str], *, connected: bool
) -> ProviderCapabilities:
    if type(granted_scopes) is not frozenset or not all(
        type(scope) is str for scope in granted_scopes
    ):
        raise ValueError("granted scopes must be a frozen set")
    if type(connected) is not bool:
        raise ValueError("connected must be a boolean")
    granted = frozenset(
        capability
        for capability, required in _SCOPES_BY_CAPABILITY.items()
        if connected and required.issubset(granted_scopes)
    )
    return ProviderCapabilities(supported=_SUPPORTED_CAPABILITIES, granted=granted)


__all__ = []
