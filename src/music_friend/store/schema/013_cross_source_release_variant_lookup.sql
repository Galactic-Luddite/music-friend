-- Cross-source variant lookup (issue #42): find_release_discovery_variant no
-- longer filters by source, so a lookup by (normalized_title, release_date)
-- alone needs its own index rather than relying on the source-prefixed
-- release_discoveries_variant_lookup index from migration 004.
CREATE INDEX release_discoveries_cross_source_variant_lookup
ON release_discoveries (normalized_title, release_date);
