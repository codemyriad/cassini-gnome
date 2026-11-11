# D58 (R79) classified refusals

Shared vectors for GNOME G13 and Mac M16. The rules are in SPEC.md, "Refusals (D58)", and
SPEC-CONTROL.md, "Result codes". `refusals.py` is an independent oracle that reproduces every
literal case. It is not production code for either app. CI can never produce a real TCC or
device denial, so each app injects the refusals and replays the cases through its own start
path, session writer and Details reader.

## writer.json

`{version: 1, decision, note, cases[], wire[]}`. A case is
`{name, profile?, companion?, request, steps[], expected}`.

- `profile` is `fallback` (GNOME: a rejected browser Start falls back to computer sound) or
  `noFallback` (Mac). A case without `profile` applies to both and has the same result on both.
  Where the profiles differ, a case is split into two with explicit profiles (for example
  `start-rejected-falls-back-to-computer-sound` and `start-rejected-without-fallback-records-nothing`).
- `companion` is the browser connection state when the case starts, for example
  `failed/buildMissing`. Setup and connection states are never refusals; the oracle ignores it.
- `request` is `{me, others: "system" | "browser" | false}`. Apps always start with both
  sides, so `me` is always true; cases turn sides off with `select`.

| op | Meaning |
| --- | --- |
| `refuse {atMs, side, code, detail?, message?}` | The side fails to open: no first buffer within 3 s, or the browser Start is refused or unanswered. |
| `select {atMs, me?, others?}` | The user turns a side on or off. |
| `fail {atMs, side}` | An opened side fails mid-call (P10). Never a refusal. |
| `stop {atMs}` | The user stops. |

A `refuse` at atMs 0 refuses a start side. A refused turn-on is a `refuse` immediately after
the `select` that turns that side on, at the same atMs (several sides may follow one select).
A `refuse` with atMs > 0 and no such `select` makes the case invalid. For `others`, a turn-on
refusal names `others` (the system tap) or `browser`. A start refusal must name a side the
profile really tries to open: a requested side, or `others` (the system tap) when a rejected
browser Start falls back. Anything else, such as `others` next to a browser refusal on
`noFallback`, makes the case invalid for that profile.

`expected` is `{folder, opened, selection, othersFrom, state, problemKind, refusals, banner}`.
A key left out is not checked. `opened` lists the sides that opened and got a track, at start
or at a later turn-on, in the order me, others, browser (`others` is the system tap, also on a
fallback); `[]` without a folder. `selection` lists the `selection` events written, as
`{atMs, me, others, reason}`, starting with the start event at 0; null without a folder.
`refusals: null` means the member is absent. `banner` is null or `{id, refusals}`.
`othersFrom` is the source of the last `othersSource` event if The others is selected at stop,
and null otherwise or without a folder. The start `selection` and `othersSource` record what
was requested, not what opened, so a system side refused at start still gives `othersFrom`
`system` (`others-unavailable-me-keeps-recording`). `state`/`problemKind` are the saved session's; cases
with a `fail` step leave them out, because mid-call loss is platform-specific (P10).

### Rules

1. Each requested side opens at 0 and receives audio until `stop`, unless a `refuse` or `fail`
   step names it.
2. A refused side records nothing and gets no track. A refused turn-on leaves the selection
   unchanged and writes no `selection` event.
3. Browser refusals at start:
   - `startRejected` with detail `busy`: no fallback, on both profiles. Nothing opens.
   - Other `startRejected`: on `fallback`, The others come from `system` (othersSource
     system/missing) while browser-captured Me stays unrecorded until the browser joins (never
     opening the native OS microphone); if `others` is also refused, nothing opens. On
     `noFallback`, no side opens.
   - `startFailed`: no side opens on either profile.
4. A folder exists if and only if at least one side opens. Without a folder, `banner.id` is
   `errors.recordFailed.line` and `banner.refusals` lists the refusals in step order. With a
   folder, `banner` is null.
5. Stored `refusals` are the `refuse` steps in step order, with only the fields given, in the
   order atMs, side, code, detail, message. An empty `message` is omitted. `detail` is kept only if it matches
   `^[A-Za-z]{1,32}$` as a whole string. `message` is cut to its first 200 Unicode code points
   (not bytes, UTF-16 units or graphemes). The cap is 16: when the list is full, each newer
   refusal replaces the 16th entry, keeping the first 15 plus the latest. The member is absent
   when the list is empty.
6. `state` and `problemKind` ignore refusals, as do `problem`, the `end` reason, `othersSource`
   and `captureOutcome`. If every opened side records until stop, the result is
   `completed`/null.
7. Checked by each app, not by the oracle: `recordingDetails.captureRecord.refusals` deep-equals
   the session list, and is present even without `captureOutcome` (with `sources: []`).

`wire` cases turn an `ok:false` Start result into the browser refusal it records (the app adds
`atMs`): `code` startRejected, `detail` the result `code` if it matches the pattern as a whole
string (known or not), `message` the result `error` cut as above (omitted when empty). A
result without a valid `code` still records `startRejected`, without `detail`, and on the
`fallback` profile still falls back (`start-rejected-without-code-falls-back`): that is the
path an app takes until results carry `code`.

"As a whole string" means the pattern must not match `"invalid\n"`. JSON Schema patterns are
ECMA-262, where `$` does not match before a final newline, but Python's `re.match`/`re.search`
(and so Python jsonschema) and ICU/NSRegularExpression do. Use `re.fullmatch` in Python and
`wholeMatch`, or `\z`, in Swift.

## readers.json

`{version: 1, readers[]}`. A reader case is
`{name, note?, stored, sources?, notSelected?, expectedLines, expectedCopy?}`.
`stored` is the session or captureRecord `refusals` value as read; `sources` are the
captureRecord source rows (default none); `notSelected` lists the sides for which the reader
would print `details.sources.notSelected`. `expectedLines` lists, in order, the Audio sources
lines that follow the source rows, which render as today and are not listed:

1. One line per distinct shown line, in first-occurrence order: `details.sources.refused.<code>`
   for permissionDenied, deviceUnavailable and startRejected, and `details.sources.failed` for
   any other code, `startFailed` included (it never reaches a file; this is only a fallback).
   Repeated entries for the same side and code, and several unknown codes for one side, print
   one line; Copy details still lists every entry. `{source}` is filled by side: me → `details.source.microphone`, others →
   `details.source.system`, browser → `details.source.browser`.
2. `details.sources.notSelected` for each listed side without a refusal line for that role. A
   `browser` refusal counts for The others, since both are the others' audio: it also hides
   `notSelected` for `others`.
3. `details.sources.empty` only when no other Audio sources line is shown at all: no source
   rows, no refusal lines and no `notSelected` lines.

`expectedCopy`, when given, lists the entries Copy details keeps: every well-formed entry, in
stored order, exactly as stored. Copy details never merges entries, re-cuts a message or
normalizes Unicode (combining and precomposed letters stay as stored); a malformed entry, such
as a 201-code-point message, is left out there too.

Reading is lenient: a `refusals` value that is not a list is ignored as a whole, and a
malformed entry is skipped. An entry is malformed exactly when the session schema's item
rejects it, applying the patterns to the whole string: it is not an object; `atMs` is missing,
not a number (a boolean is not a number) or outside 0..86460000; `side` or `code` is missing or
does not match `^[A-Za-z]{1,32}$`; `detail` is present, not null and does not match; or
`message` is present, not null and not a string of at most 200 code points. A null `detail` or
`message` means absent. A well-formed entry with an unknown side is not shown; `detail`,
`message` and unknown members never change the lines; every well-formed entry stays in Copy
details. Counting is in Unicode code points throughout: an emoji counts once (not as two UTF-16
units), and a combining mark counts as its own code point.

`tests/test_refusals.py` checks the oracle against every literal case and validates every
expected stored and banner list against `schemas/session.schema.json`. It also checks that each
of these mutations fails at least one case: a refusal written for a `fail`; a folder kept when
nothing opened; a refused side that still gets a track; a refusal that changes `state`;
`message` dropped, not cut, cut at UTF-16 units, UTF-8 bytes or graphemes, or kept when empty
(writer or wire); a `noFallback` fallback; a fallback after `startFailed` or
`startRejected/busy`; a fallback that drops Me or needs a detail; an others refusal that ends
everything or nulls `othersFrom`; the cap dropping the latest entry, keeping the last 16, or
missing; a refused turn-on (of Me or The others) that changes the selection or writes a
selection event; a malformed or newline-terminated result `code` kept; `notSelected` or `empty`
printed next to a refusal line; an unknown side shown; an unknown code or a stored
`startFailed` without the `failed` line; repeated lines; a reader that keeps entries with a bad
`atMs`, pattern-invalid fields, a bad detail or message, or rejects a null detail; a reader
that counts a message in UTF-16 units, UTF-8 bytes or graphemes; Copy details that normalizes
to NFC, re-cuts at UTF-16 units or keeps a malformed entry; a `browser` refusal that leaves
`notSelected` for The others; `empty` printed next to `notSelected` lines. It also
checks that the reader's malformed rule agrees with the schema on every reader entry. The `resume` op and `resumeRefused` vectors come with #169.
