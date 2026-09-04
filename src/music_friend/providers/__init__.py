"""Provider-neutral music source contracts."""

from music_friend.providers.base import (
    Capability,
    HealthStatus,
    MusicSource,
    Page,
    ProviderCapabilities,
    ProviderHealth,
    require_capability,
)

__all__ = [
    "Capability",
    "HealthStatus",
    "MusicSource",
    "Page",
    "ProviderCapabilities",
    "ProviderHealth",
    "require_capability",
]
