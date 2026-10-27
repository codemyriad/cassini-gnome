# PR6 review disposition

Reviewed all 38 findings and 11 parity findings. D6/D6.1 is authoritative where a
finding proposed several possible behaviours; D7 prioritises common visible
behaviour and real bugs. S-3a expectations were published before the app update.

| GNOME findings | Resolution and evidence |
|---|---|
| 1, 4, 6, 8, 12, 27 | R2/R3 status precedence, whole-document schema validation, nullable ETA, actionable-error preservation and failed retry. Shared r2/r3/r9, states-v2 and status-matrix unit tests. |
| 2, 5, 24 | R8 consecutive time-sorted turns, no seam/asides, exclusive 600000 boundary, whitespace and override quote rules. Shared r8/main, override/status regression. |
| 3 | Worker scans, incremental folders, stat-keyed parsed/derived caches, stable watches, trailing debounce/max wait. Main-context heartbeat, read-count and watch identity tests. |
| 7, 14, 35 | R6 trimmed title chain, Untitled meeting, Date unknown and explicit sort/group rules. Shared r6/main; unknown-duration formatting tests. |
| 9, 17, 18, 29 | Canonical lowercase folder names, lowercase activity lookup, UUID + basename internal key/tie-break per D6.1. Shared r1/r4 and duplicate-prefix regression. |
| 10, 25 | R7 speaking speakers only, first-speaking numbering/colour, received labels. Shared r7/main. |
| 11, 21, 30 | Unpublished/schema-invalid Received folders skipped, non-null received context required. Shared r1/r9; publish-after-watch regression. |
| 13, 32 | R4 explicitly chooses read-only salvage for unsupported/mismatched capture manifests. Existing D3 recovery tests; no source writes. |
| 15 | Format-v3 switches on rounded MB and uses decimal half-up for GB. All 69 unchanged format cases pass. |
| 16, 31 | Tail-granule/pre-skip duration independent of body trust; unchanged Opus header/digest/words cached. Cold integrity check is retained because R3 requires stale-body rejection, off the main thread. Shared stale-audio/main assets and no-reparse test. |
| 19, 20, 23 | Root-link/failed-vs-empty semantics and regular, nonblocking no-follow reads. Real FIFO subprocess deadlines and root/symlink tests. |
| 22, 33 | No invented zero when no track counts. R4's unreadable-session history follows the binding rule, including with activity; badge precedence remains independent. Shared r4/r5 and stable unreadable-row tests. |
| 26 | Newer app metadata ignored/retained/read-only with unsupported-version; no destructive rewrite or write-down offered. Existing future-version regression. |
| 28 | Strict sidecars; embedded items follow the portable reader exactly, as D6 R3 explicitly requires. No new clamping. |
| 34 | Invalid offset counts as display zero, not a dropped track. D6.1 exact floor of the full rational sum. Shared r5 and fractional-end tests. |
| 36, 37, 38 | Unknown/stale portable bodies unreadable; absent/ignored own metadata uses sidecar fallback; strict JSON plus full schema validation. Shared r2/r3, schema differential tests. |

| Parity findings | Resolution |
|---|---|
| 1, 10 | R7 speaking-only, first-speaking order/number/colour, received labels; no unused metadata speakers. |
| 2, 7 | R3 explicit transcript ID/file, integrity/unknown-format body rejection, TITLE fallback and independent Opus duration. |
| 3 | R2 intentionally chooses whole-file invalidation, followed by own sidecar fallback; no per-field salvage. |
| 4 | Stored actionable error survives derived damage. |
| 5 | R8 chooses consecutive runs for the row quote; transcript-view seam heuristics are intentionally not reused. |
| 6 | Format-v3 fixes rounded-size boundary and GB half-up. |
| 8 | Out-of-range offset becomes unknown/display zero while retaining PCM. |
| 9, 11 | Canonical lowercase folder names and D6.1 identity. |

The lighter suite does not attempt exhaustive cross-platform parity for every
malformed/rare input. No UI surface or new user-facing copy was added. All new
copy comes from D6/catalog or the existing vectors. Mac independently reviews
S-3a expected files; corrections must go through the shared source before copying.

## Portable descriptor correction after the production-reader review

The original generated Onboarding asset omitted required descriptor tags, and
GNOME's reader failed to require the complete manifest descriptor. The shared
builder now writes every required producer descriptor (manifest/schema, identity,
transcript index/chunk MIME, audio fields and decode hint). The corrected asset,
pins and both received full-file hashes are published at coordination 72862aa;
all expected board JSON files remain byte-identical.

The reader now requires nonempty manifest encoding/count/hash/raw/compressed
length/MIME/schema descriptors, matching Mac's manifest-descriptor validation.
Copies and optional summary tags do not become requirements for decoding a
valid manifest. Regression tests remove or empty each required descriptor;
actual Received Opus tests show damaged transcript, preserved audio/duration,
import-date fallback, no quote/names and no source-file writes. Published format
conformance vectors still pass unchanged. S-3a's independent verifier now checks
all required producer descriptors, without relying on either app reader.
