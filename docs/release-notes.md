# Writing release notes

Cassini has two changelogs for two different readers:

| | Developer changelog | Release notes |
|---|---|---|
| **Reader** | Developers and anyone debugging a build | People using Cassini and testers deciding what to try |
| **Source** | Merged PR titles on `main` (automatic) | `release-notes.d/<short-slug>.md` (test builds) and `NEWS` (stable releases) |
| **Says** | Every change | What you will notice, and anything you need to do |
| **Shown** | Folded `<details>` section at the bottom of the GitHub pre-release | Top of the GitHub (pre-)release and GNOME release description |

Release notes never contain PR titles, `#123` numbers, decision IDs (`D95`), file names, `CI`, tests, refactors, or build tooling.

## 1. Per-PR fragments (`release-notes.d/`)

Every pull request that changes something a person using Cassini can notice adds **one** file `release-notes.d/<short-slug>.md`:

```markdown
kind: new | better | fixed
---
One or two plain sentences: what you can do now, or what is better, in the words on screen.
```

- **Skip invisible work:** CI, tests, refactors, internal performance work, and bugs both introduced and fixed since the last release with notes add **no** fragment.
- **Words on screen:** Write in English, sentence case, using Cassini’s on-screen terms (`meeting`, `Library`, `Saved voices`, `Record`, `Stop`, `written down`). Never write “please”, marketing filler, or straight ASCII apostrophes (use `’`).

## 2. Unattended daily test builds (`09:00`, `14:00`, `20:00` Europe/Berlin)

`build-aux/ci/daily_test_release.py` collects `release-notes.d/*.md` fragments added or updated since the `baseline` commit of the previous published release (tracked automatically via the published pre-release / `gnome-daily-test-release` deployment history and stable tags, so no manual state-file commit is needed on `main`):

- Renders up to three short groups in priority order — **New**, **Better**, **Fixed** — within **500 characters**.
- If all unpublished fragments exceed 500 characters, keeps the **New** items first and ends with `and smaller fixes`.
- If there are no unpublished fragments, publishes:
  `Fixes and improvements under the hood. Nothing changes in how you use Cassini.`
- Appends the automatic PR list only inside a folded `<details><summary>Developer changelog</summary>…</details>` block beneath the user notes on the GitHub pre-release.

## 3. Stable releases (`NEWS` and `release.yml`)

Before tagging a stable release `vX.Y.Z`, curate the top `Version X.Y.Z` section of [`NEWS`](../NEWS) by hand from all `release-notes.d/` fragments accumulated since the previous stable release:

1. **Sort** entries into *Highlights* (1–5 things worth mentioning to a colleague), *Before you update* (any action, changed default, or logout/restart step), *Improvements* (grouped by area in the app), *Fixes* (only bugs someone on the previous stable release could hit), and *Leave out*.
2. **Collapse** multiple fragments touching the same part of Cassini into one plain outcome, naming settings and buttons exactly as they read on screen at the release commit.
3. **Clear** the published `release-notes.d/*.md` fragments in the release preparation PR once folded into `NEWS`.

`build-aux/ci/release_notes.py` (run by `.github/workflows/release.yml` before publishing) refuses any stable tag whose `NEWS` section is missing, still contains placeholder text (`Not released yet`), or leaks developer markers (`#123`, `D95`, `CI`).
