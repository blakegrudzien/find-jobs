# Handoff — 2026-09-07

State and open items for the next session. Durable project context (who this
is for, what every scoring weight means, what not to change) lives in
`CLAUDE.md` and is loaded automatically — **read that first**; this file is
only what's in flight.

## Repo state

Clean, everything committed, nothing pending.

```
ab50d4c  removed board size as headcount proxy
eebaea4  Recalibrate scoring and filter to Bay-Area-or-remote
953daf3  Job board scraper for new-grad engineering roles
```

Public at https://github.com/blakegrudzien/find-jobs.

**Standing rule: never `git commit` or `git push`.** Prepare changes in the
working tree; Blake reviews and commits them himself.

## Last run (2026-09-07, current code)

```
483 roles from 140 companies
 21 strong fit (60+)
 91 worth a look (45-59)
386 Bay Area
 23 flagged for crunch language
  1 duplicate JD collapsed
 10 dead slugs (run --prune to comment them out)
```

Top of the list is essentially all explicit new-grad / early-career Bay Area
roles, and 0% of the top 100 require more than 2 years.

---

## Open items, highest value first

### 1. `README.md` is out of sync with the code — public repo

Three concrete errors, all introduced when board size was removed from
scoring:

- **line 36** — "a posting has to clear three gates", then lists **four**
  (title, experience, graduation year, location).
- **lines 53–54** — still lists "company size (proxied by how many roles the
  board has open)" as a scoring input. It isn't one any more.
- **lines 101–102** — still lists "Board size is a weak proxy… so it's
  weighted lightly on purpose" as a known limitation. It's now not weighted
  at all.

This is the file recruiters would actually read, so it's the first thing
worth fixing. The correct replacement for the limitation bullet is the
reasoning already written up in `CLAUDE.md` under "Board size is
intentionally NOT scored".

### 2. Score bucket thresholds were never re-decided

Blake picked "60+ strong / 45–59 worth a look" back when board size was still
contributing a +6 that most rows collected. Removing it compressed the whole
distribution:

| | before | after |
|---|---|---|
| strong (60+) | 29 | **21** |
| worth a look (45–59) | 132 | **91** |

21 may well be the better "start here" size given he reads top-down until ~5
consecutive misses. If he wants the *same volume* as before, the equivalent
cut is roughly **55 / 40**. He hasn't decided — ask, don't assume.

### 3. Claude Code daemon is dead — blocks Remote Control

Not a repo issue, but unresolved:

- `~/.claude/daemon.lock` points at PID **16390**, which is not running.
- `~/.claude/daemon.status.json` last written **2026-07-26**, `workers: {}`.
- Lock file records version 2.1.220; installed CLI is 2.1.263.
- No `daemon.log` exists at all.

Suggested fix (Blake to run): `rm ~/.claude/daemon.lock
~/.claude/daemon.status.json`, then fully quit and reopen Claude Code.
Caveat: that the daemon is dead and the lock is stale is **verified**; that
Remote Control specifically depends on this daemon is **inferred** from the
file layout, not confirmed.

### 4. VS Code extension version skew

Three windows open, each pinned to the extension version it loaded at
startup, because VS Code doesn't hot-swap an extension in an open window:

| Workspace | Extension version |
|---|---|
| `Developer/chess-rag` | 2.1.258 |
| `Developer/Personal/Portfolio` | 2.1.259 |
| `Job_Search/jdScrape` | 2.1.263 (current) |

VS Code has marked 2.1.207 / .258 / .259 / .261 obsolete but can't delete them
while they're loaded. Fix: `Cmd+Shift+P` → **Developer: Reload Window** in
each window. This is also why a session started in one window is not
selectable from another window's sidebar — sessions belong to the extension
host that spawned them.

### 5. Missing file

`job-search-handoff-v4.md` is referenced twice in
`find-jobs-scraper-writeup.md` (in `~/Downloads`) but has never been provided.
It reportedly holds the Notion tracker schema and the culture-screening
criteria behind `CRUNCH_FLAGS`. Ask for it before touching anything that
depends on those.

---

## Already measured — do not redo these audits

A previous session burned real time deriving these. They're settled:

- **Years regex leak**: across 5,568 live postings from 90 boards, the old
  pattern let **20.6%** of over-experienced roles through — **24% of the top
  50**. Four causes: gap between "years" and "experience" capped too short;
  ranges read from the bottom end (`1-3 years` → 1, which then collected the
  *largest* years bonus); en dashes unmatched; many requirements never say
  "experience" (`3+ years building product features`). Now 0% of the top 100.
- **`" i"` substring bug**: matched any word starting with i, so "Software
  Engineer, **I**nfrastructure" and "**AI** Backend Engineer" took the full
  +20 new-grad title bonus — **287 of 1,783 rows (16%)**.
- **Board size is not a headcount proxy**: measured across 402 live boards.
  Boards with ≤5 open roles are netlify, dremio, motive, prisma, gremlin —
  established companies hiring selectively, not small teams. Aggregate trend
  is real but weak (8.6% of postings on tiny boards use founding-engineer
  language vs 1.5% on the largest). `TOO_EARLY_SIGNALS` measures the same
  thing directly. **Do not reintroduce it.**
- **Cross-file duplicate slugs**: 30 of them (18 greenhouse+ashby, 2
  greenhouse+lever, 10 ashby+lever). `--show-dupes` lists them; `dedup_key`
  collapses identical JDs.

## Session/context notes

- This project directory has only ever had **one** Claude Code session. The
  terminal session Blake remembered was in `~/Developer/chess-rag`; the
  Desktop app is a separate product whose chats Claude Code cannot read.
  There was nothing to merge.
- Transcripts live in
  `~/.claude/projects/-Users-blakegrudzien-Job-Search-jdScrape/`.
- The persistent memory directory for this project is empty. `CLAUDE.md` is
  doing that job instead.

## Suggested first move

Fix the three `README.md` errors in item 1, then ask Blake about the bucket
thresholds in item 2. Both are small and unblock the rest.
