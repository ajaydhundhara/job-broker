# Replay analysis: candidate observations (for author review)

**Status: for author review.** These are candidate observations only, not
conclusions and not a scheduler recommendation; the author writes those
under "## Analysis" in [replay_results.md](replay_results.md). Every figure is
copied as printed from a table in replay_results.md (§ = its section), and
`tests/test_replay_candidates.py` checks that each one still appears there.

Read with these limits: 5 seeds (42–46) of one trace week; each cell is a
median taken on its own, so cells can come from different seeds; "seeds
fair < / = / > fifo" compares the same seed; the burst section and the
fair vs fifo table are at idle timeout 10 min only; the sweep is fair only.

## fair vs fifo

Candidate observations, for author review:

- Grand total: fair $3,167.67 vs fifo $3,130.37; seeds `3 / 0 / 2`, and the
  ranges overlap ($3,062.65–$3,185.46 vs $3,066.40–$3,183.60) (§3).
- Wait p90, all: fair 122.7 vs fifo 116.8 min; seeds `1 / 1 / 3` (§3).
- Wait p90, platform: fair 8.0 vs fifo 90.7 min; seeds `4 / 1 / 0`, fair never
  higher. Burst wait p90, platform: 16.4 vs 117.7 min, seeds `4 / 1 / 0` (§3).
- Wait p90, vision: fair 143.1 vs fifo 136.3 min; seeds `1 / 1 / 3` (§3).
- Started ≤ 10 min, speech: fair 87.5% vs fifo 89.3%; seeds `5 / 0 / 0`, i.e.
  fair lower in every seed (§3).
- Started ≤ 10 min, platform: fair 91.7% vs fifo 87.5%; seeds `0 / 2 / 3` (§3).
- Started ≤ 10 min, all: fair 60.9% vs fifo 61.3%; seeds `2 / 2 / 1` (§3).
- Idle share: fair 28.2% vs fifo 27.2%; seeds `3 / 0 / 2` (§3).

## the burst window

Candidate observations, for author review:

- 176 jobs were submitted in the window under both policies; the trace has
  vision 178, platform 3 and speech 3 rows in it (§1, §5).
- The queue is 0 (0–4) at 2025-03-05T06:00Z and 150 (149–156) at
  2025-03-05T06:15Z under fair; the peak, 151 (149–157), is reached 80.0
  (75.0–85.0) min after the window start under fair and 85.0 (75.0–85.0) under
  fifo, not at its start (§5).
- At the window end (2025-03-05T08:00Z) the queue is still 65 (28–89) under
  fair and 58 (44–89) under fifo (§5).
- Recovery, all: fair +82.8 (+29.4–+105.5) vs fifo +69.2 (+55.8–+96.0) min after
  the window end; seeds `1 / 1 / 3` (§5).
- Recovery, platform: fair -37.0 vs fifo +53.7 min, seeds `4 / 1 / 0`; under
  fair the last platform burst job started before the window ended in the
  median seed (§5).
- Recovery, vision: fair +69.3 vs fifo +56.4 min; seeds `1 / 1 / 3` (§5).
- The median queue is 0 from 2025-03-05T09:30Z under fair and from
  2025-03-05T09:15Z under fifo; at 09:30Z the largest seed still has 8 (fair)
  and 3 (fifo) queued (§5).
- Vision's peak queue is 151 over the week and peak nodes reach 10 for all
  teams together, under both policies (§2).

## idle timeout: cost vs wait

Candidate observations, for author review:

- Grand total by idle timeout (fair): 2 min $2,965.00, 10 min $3,167.67,
  30 min $3,348.16; the ranges do not overlap ($2,867.92–$3,017.54,
  $3,062.65–$3,185.46, $3,279.33–$3,421.28) (§4).
- Idle share: 23.3%, 28.2%, 32.1% (§4).
- Nodes launched: 208, 179, 145 (§4).
- Wait p90, all: 123.8, 122.7, 123.5 min, with overlapping ranges (83.9–145.9,
  91.8–156.6, 117.7–131.7) (§4).
- The sweep reports only all-team wait p90; per-team waits by timeout are in
  replay_results.json, not in a table (§4).

## what we would change

Candidate observations, for author review (facts an author may weigh; no
change is proposed here):

- Vision has 255 of 478 jobs and the lowest started ≤ 10 min, 36.1% (fair)
  and 36.5% (fifo); every other team is at 87.5% or more under both (§2).
- Peak nodes for all teams is 10, the node cap, under both policies (§2).
- Team idle cost under fair is $892.05 of $3,153.54 charged to teams; idle
  share by team ranges from 10.2% (speech) to 45.5% (platform) (§2).
- Never-ready provider overhead is $3.50 (fair) and $3.38 (fifo) of grand
  totals $3,167.67 and $3,130.37 (§2).
- Capacity refusals: 38 (fair) and 39 (fifo) over the week (§2).
