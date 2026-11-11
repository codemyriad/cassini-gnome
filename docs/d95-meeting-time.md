# Meeting time (D95)

The format pin is `32f37a6200c88bdd4d23c1f44497f1761f3401da`. Conformance
027–029 test the same reader used by the Library and standalone Reader.

`meeting_time.selected()` chooses the manifest's `recordedAtUtc`, then an
absolute DATE that differs from `createdAtUtc` as an instant, then the
zone-less `recordedAtLocal` label. Mirror tags never override the manifest.
The instant is rendered in the viewer's zone. A local label is formatted with
its written calendar/time fields; it never acquires a fabricated instant.

The Library keeps `started_at_ms` for real recording instants and
`recorded_at_local` for the fallback label. `display_start` feeds list, header,
Search and Markdown formatting. `sort_time` uses a wall label's calendar fields
for ordering, then the document's creation instant as the final fallback.
Search stores wall labels separately, compares their written calendar dates,
and never treats the seal-only sort fallback as a date-filter match. Its cache
upgrades on open; no user files are migrated.

Both capture publication and export write the known playback-zero instant as
`recordedAtUtc`, `CASSINI_RECORDED_AT_UTC` and DATE, preserving milliseconds.
An unknown start omits the field/mirror and uses creation time in DATE. Only a
recorded site offset permits generating `recordedAtLocal`; the export device's
zone cannot supply missing provenance. Received local-only labels stay labels.

## Shared Library vector amendment proposed to the coordinator

The source bytes in `ux/library-vectors` remain unchanged. The GNOME test
adapter applies these D95 amendments pending the shared source update:

- `r3-transcript-precedence`: received rows 5/6 have DATE equal to seal time;
  their starts become null and their group becomes Date unknown.
- `r9-received-once`: both starts become null, in one Date unknown group. Row 2
  sorts before row 1 because its known seal is a sort fallback; plain row 1 has
  no known instant.
- `main`: received row 6 has only creation time; start becomes null and its
  group becomes Date unknown. Its seal-only sorting position stays the same.
- `received-only`: the same row-6 amendment.

All other row content is checked against the original goldens. Independent
D95 oracles cover three new format vectors, nine-minute sealing and actual
publication/export/import, offset-equivalent seal timestamps, local labels
across Honolulu/Tokyo, Search cache invalidation and standalone subtitle dates.
Production mutations selecting seal time in the reader, producer or DATE tag
each fail their targeted regression test.
