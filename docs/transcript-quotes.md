# Turn quotation marks (D38.5)

When a turn is displayed, straight double quotes pair in reading order and become “ and ”. A final quote without a partner in that turn disappears, and an item left with only spaces disappears with it. Pairing never crosses a speaker turn. Existing curly quotes and apostrophes stay as written.

Library previews, the Meeting transcript, the standalone transcript reader (including its in-window search), Search excerpts and Markdown export use this display projection. The timed items and original transcript files are unchanged. Even when a quote-only item disappears, transcript links and Search seek positions still address the original item.

The Meeting transcript splits a long speaker turn into paragraphs of at most 100 items or 30 seconds. Quotes still pair across the whole turn, so a quote opened in one paragraph closes in the next, as it does in Search and export.

The Library pull quote (R8) is not turn text: it stays verbatim, straight quotes included (LIBRARY-RULES R8.3, shared vector `r8-consecutive-quote`).

The formatting tests cover embedded pairs, pairs across timed items and across paragraph bounds, an orphan at a speaker boundary, quote-only items in the reader layout and search, and a quote-only item before a Search match. The Markdown goldens remain unchanged for their existing inputs.
