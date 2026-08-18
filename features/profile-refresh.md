# Keeping the mandate profile true: the refresh nobody has to remember

prefix: PRF

**Type:** feature
**Complexity:** 3
**Estimated Duration:** ~3 days
**Risk:** medium
**Scope:** newspulse, profile
**Test Strategy:** unit tests with an injected `generate` over the due/not-due logic, the change classifier and the proposal store, using a frozen clock passed in rather than patched; a test that the daily job runs at most the configured number of refreshes and skips a client whose profile was touched by hand yesterday; FastAPI `TestClient` coverage of the review surface including accept, discard and accept-all. No test reaches a model or the network.

## Context

PR-01 builds a living knowledge model of each client: business, people, products,
positioning, topics, risks. The engine exists. `profile.py` holds fourteen fields,
each stored as a `ClientFact` carrying its source URL, its source title and whether
a human or a model put it there, and "Mit KI ausfüllen" researches them from the
web on demand.

The word the product definition uses for what is missing is "laufend". Today the
profile is filled once, usually in the week a mandate starts, and then it is a
snapshot with a date on it. A CEO leaves and the letters keep naming them. A
company raises a round, launches a product, changes what it sells, and every
generated text still argues from last spring. The profile does not decay loudly:
it decays into confident, well written, wrong sentences, which is the worst
failure mode a PR tool has.

The fix is not "re-run the research weekly and overwrite". `ClientFact.filled_by`
exists precisely because a fact the consultant knows from a kick-off call and a
fact a model read on a website must never be confused, and an automatic refresh
that silently replaces the first with the second would destroy the more valuable
of the two. So the refresh proposes and never writes: it is grade F, the same as
PR-01's initial fill, and its output is a small pile of "this looks different now"
waiting for a yes.

## Summary

Today a mandate profile is filled once and then quietly ages. Nothing re-reads it,
so a departed CEO stays in the profile and therefore in every pitch, and the only
way anyone finds out is that a journalist mentions it.

After this build, the profile refreshes itself in the background on a rhythm, a
few mandates a day rather than all of them at once, and never writes anything. It
comes back with what changed: this field said X, the web now says Y, here is the
source. The consultant accepts or discards each one, and what he accepts becomes a
fact stamped as his. Facts he entered by hand are never overwritten and never
quietly proposed away, only flagged as contradicted, because the tool knowing a
website is not the same as the tool knowing better. Each mandate carries the date
it was last checked, so a stale profile is visible instead of assumed fresh.

## Decisions

### DEC-1: What rhythm does the refresh run on?  [options]
**Status:** open
**Recommend:** C
**Locks as:** the scheduling rule in PRF-01
- **A · Feste Frequenz je Mandat** Every profile is re-researched every 30 days, oldest first. Predictable, trivial to reason about, and easy to explain to a client. It also spends the same effort on a mandate that changes monthly and one that has not moved in two years, and the daily budget is set by the size of the portfolio rather than by need.
- **B · Auf Zuruf, mit sichtbarem Alter** No automatic run at all. Every profile shows how old it is and offers a refresh button, so the consultant refreshes what he is about to work on. Zero cost, zero surprise, and it fails exactly the way the current state fails: the profile you forgot about is the one that is wrong.
- **C · Anlassgesteuert, mit einer Obergrenze pro Tag** A profile becomes due when its own coverage suggests something moved: an executive-change or financial item in the archive for that client, a new alert, or simply 60 days without a check. At most a handful run per day, oldest-due first. Spends effort where the news says something happened, and the age fallback means a quiet mandate still gets looked at. Costs a due-check that has to be right, since a bug there means either nothing refreshes or everything does.

### DEC-2: What happens to a fact a human entered when the web disagrees?  [options]
**Status:** open
**Recommend:** A
**Locks as:** the conflict rule in PRF-02
- **A · Nie ersetzen, nur widersprechen** A human-entered fact keeps its value and gains a visible "the web says something else" marker with the source. Nothing changes until the consultant acts. The profile stays trustworthy by default, and a wrong hand-entered fact can survive for a while, visibly.
- **B · Wie jeder andere Vorschlag behandeln** A contradiction becomes an ordinary proposal in the review pile with the old value shown beside the new one. Simpler, one code path, and it treats a fact from a kick-off call as equal to a sentence scraped from an about page.
- **C · Nach Feldern unterscheiden** Hard facts like headcount, revenue and legal form may be proposed over; soft ones like positioning and spokespeople may not. Closest to how a consultant actually thinks, and it needs a per-field policy that has to be maintained as the field list grows.

## Stories

### PRF-01: Decide which profiles are due, and refresh them in the background

The scheduler side. A pass over the portfolio picks the profiles that have earned
a look, runs the existing research for each, and stores what comes back as
proposals. It writes nothing into the profile itself.

Bounded on purpose: a handful per run, oldest-due first. The research call is a
web search plus a model, so an unbounded pass over sixty mandates would be both
expensive and a good way to get rate-limited on the day it matters.

**Changes:**
- New `src/newspulse/profile_refresh.py`: `due()` over the portfolio, `refresh()` for one client reusing `profile.research()`, and `run()` with a per-run cap
- Persist proposals instead of holding them in memory: a `ProfileProposal` model in `src/newspulse/models.py` (`client_id`, `key`, `value`, `source_url`, `source_title`, `previous_value`, `proposed_at`, `proposed_by`), replacing the in-memory dict in `web/routes/profile.py`
- Alembic revision `0022_profile_proposals`
- Add `profile_checked_at` to `Client`, following the existing `impulse_checked_at` precedent
- Call `profile_refresh.run()` from the daily sweep in `src/newspulse/job.py`, guarded so a failure never fails the sweep

**Acceptance:**
- A profile is due under the locked rule from DEC-1 and not otherwise; the due check is a pure function of stored state and an injected clock, with no wall-clock reads.
- A run refreshes at most the configured number of clients and takes the oldest-due first, so a large portfolio drains over days rather than in one burst.
- Proposals survive a restart, which the current in-memory dict does not, and a second refresh of the same client replaces that client's outstanding proposals rather than stacking duplicates.
- A refresh writes nothing to `client_facts`; a test asserts the stored facts are byte-identical before and after.
- `profile_checked_at` is set on every attempt, including one that produced no proposals, so "checked and nothing changed" is distinguishable from "never checked".
- A research call that fails leaves the client's existing proposals and facts untouched, logs at ERROR, and does not stop the remaining clients in the run.
- With no clients due, the run is a no-op and the daily sweep is unaffected.

**Files:** `newspulse/src/newspulse/profile_refresh.py` (new), `newspulse/src/newspulse/models.py`, `newspulse/migrations/versions/0022_profile_proposals.py` (new), `newspulse/src/newspulse/web/routes/profile.py`, `newspulse/src/newspulse/job.py`, `newspulse/tests/test_profile_refresh.py` (new)

**Smoke:** `pytest tests/test_profile_refresh.py tests/test_profile.py tests/test_job.py` passes

### PRF-02: Review what changed, and decide what is true
**Depends on:** PRF-01
**Decisions:** DEC-2

The human side, on the profile page that already exists. What changed since last
time, each with its old value, its new value and its source, and one decision per
row.

A field the consultant filled himself is handled under the locked rule from DEC-2.
Whatever that rule is, the page must make the provenance of every value legible at
a glance, because the whole point of `filled_by` is that a reader can tell who
believes what.

**Changes:**
- Render outstanding proposals on `client_profile.html` as a review block above the field list: old value, new value, source link, and accept or discard per row
- Add accept-all and discard-all for a client, since a refresh that finds twelve small changes should not cost twelve clicks
- Extend `POST /client/{id}/profil/accept` and `/discard` in `web/routes/profile.py` to work from the stored proposals, stamping an accepted fact with `filled_by` set to the human
- Show `profile_checked_at` on the profile page and on the client list, as an age rather than a date when it is old
- German strings into `i18n._EN`

**Acceptance:**
- Each proposal shows the current value, the proposed value and a working link to the source it came from; a proposal with no source is not shown at all.
- Accepting writes the new value with `filled_by` set to the human, not to the model, because the human is who decided it.
- Discarding removes the proposal and does not re-propose the same value on the next refresh of that client.
- A conflict with a human-entered fact behaves exactly as DEC-2 locks it, and the page states which rule is in force rather than leaving the reader to infer it.
- Accept-all and discard-all act only on the proposals currently shown for that client, and a proposal that arrived between render and submit is not silently swept up.
- The profile page and the client list show when the profile was last checked, and a profile never checked says so rather than showing a blank.
- Every new German UI string has an English entry in `i18n._EN`.

**Files:** `newspulse/src/newspulse/web/templates/client_profile.html`, `newspulse/src/newspulse/web/routes/profile.py`, `newspulse/src/newspulse/web/templates/clients.html`, `newspulse/src/newspulse/profile.py`, `newspulse/src/newspulse/i18n.py`, `newspulse/tests/test_profile_refresh.py`

**Smoke:** `pytest tests/test_profile_refresh.py tests/test_profile.py` passes

## Deferred

- **Refreshing from the archive rather than the web.** The tool already reads
  everything published about a mandate. An executive change that appeared in the
  coverage two weeks ago is a better refresh trigger and a better source than a
  web search, and it costs no external call. Worth doing once the trigger side of
  DEC-1 has proven itself.
- **Telling the consultant a fact went stale in a text he already sent.** If the
  CEO changed on Tuesday and a letter naming the old one went out on Monday, that
  is worth knowing. It needs the outreach ledger to exist first.
- **Field-level confidence.** Some facts age in months and some in years, and the
  refresh treats them alike. A per-field half-life is the obvious next tuning knob
  and it needs real refresh data to set honestly.
