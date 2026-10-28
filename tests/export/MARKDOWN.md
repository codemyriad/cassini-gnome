# Markdown export (S-5): binding for both apps

UTF-8 without BOM, LF line endings, and exactly one trailing newline. The input is the meeting model after
LIBRARY-RULES: speaker display names per R7/R7.3, and items in `startMs` order (ties: file order).

```
# {title}

{Weekday} {d} {Month} {yyyy}, {HH:MM} · {duration}
                                   (local time zone; duration per format-vectors meetingDuration;
                                    " · {duration}" is omitted when the duration is unknown)

Speakers: {name}, {name}, …        (speaking speakers in first-speaking order; this block is omitted if there are none)

## Transcript

**{speaker}** · {HH:MM:SS}         (one block per turn: consecutive items of the same speaker, as in R8)
{the turn's item texts joined with one space, whitespace collapsed}

… (one blank line between turns)

## Notes                           (only when meeting.json has notes)

- {HH:MM:SS} {note text}           (in atMs order)
```

- **Text normalisation:** collapse every run of whitespace to one space, then trim.
- **Escaping:** inside text (title, names, transcript, notes), put a backslash before each of these characters:
  backslash, backtick, `*`, `_`, `[`, `]`, `<`, `>`. Also escape what would start a Markdown block at the start of a
  line: a leading `#`, `-` or `+` gets a backslash before it, and a leading `<digits>.` gets one before the dot
  (`1\.`).
- **Not exported:** voiceprints, the "auto" provenance and colours (HARMONY 9). Names are exported as shown.
- **File name:** `{title}.md`.
  - Replace `/`, `\` and control characters with `-`, trim, and cut to 120 characters.
  - An empty title becomes `Untitled meeting.md`.
  - On a collision, use `{title} 2.md`, `{title} 3.md`, and so on.

Golden cases are in `markdown-cases.json`, with input and expected output. `budget-review.md` is the first case's
output, for reading.
