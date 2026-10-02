"""Provider-neutral music source contracts."""

from music_friend.providers.base import (
    Capability,
    HealthStatus,
    MusicSource,
    Page,
    ProviderCapabilities,
    ProviderHealth,
    RecentPlay,
    RecentPlaySource,
    require_capability,
)

__all__ = [
    "Capability",
    "HealthStatus",
    "MusicSource",
    "Page",
    "RecentPlay",
    "RecentPlaySource",
    "ProviderCapabilities",
    "ProviderHealth",
    "require_capability",
]
