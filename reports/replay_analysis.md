How to read this: 5 seeds of one trace week. A difference counts as real only
if it holds in every seed ("seeds fair < / = / > fifo" in §3 and §5 compares
the same seed); medians alone can mislead because each cell's median may come
from a different seed. Every figure below is copied from the tables above,
except node prices and the fallback limit, which are the configured values
(`broker/config.py`). Two facts are not in the tables and are marked *(checked on the replay
databases)*: they were read from the per-seed SQLite files of this run.

### fair vs fifo

**On cost and overall wait, the two policies are not distinguishable.**
The grand total is $3,167.67 under fair and $3,130.37 under fifo, but fair
is cheaper in 3 seeds and dearer in 2 (`3 / 0 / 2`), and the seed-to-seed
range ($3,062.65–$3,185.46 for fair) is wider than the gap between the
medians. The work itself costs the same: busy cost is $2,263.78 vs $2,266.19
(§2). The whole difference is idle cost ($892.05 vs $852.71), and its sign
changes with the seed (idle share `3 / 0 / 2`). One reason it moves at all:
boot times and launch failures are drawn per launch number, and the two
policies launch different nodes, so one seed gives them different draws
(README, "Seeds"). Overall wait p90 is 122.7 vs 116.8 min with seeds
`1 / 1 / 3`, and the share started within 10 minutes is 60.9% vs 61.3%
(`2 / 2 / 1`): no consistent winner.

**Fair protects a light team that arrives during another team's burst.**
Platform's wait p90 is 8.0 min under fair and 90.7 min under fifo; fair is
lower in 4 seeds and never higher (`4 / 1 / 0`). Platform has only 24 jobs,
so its p90 is set by a few slow ones: its 3 jobs inside the burst window
wait 16.4 min (p90) under fair and 117.7 min under fifo, again `4 / 1 / 0`.
Under fifo they queue behind vision's backlog in arrival order; under fair a
team with less GPU usage in the last 24 h goes first within a priority.

**Fair costs the heaviest team a little, in every seed.** Speech starts
87.5% of its jobs within 10 minutes under fair and 89.3% under fifo, lower in
all 5 seeds (`5 / 0 / 0`), the only fair-vs-fifo effect that holds in every
seed. Speech has by far the most GPU-hours (985.6 of 2,092.4, §2), so fair
puts it behind other teams' jobs of the same priority. *(Checked on the
replay databases: at 37 of its 56 submissions speech has the highest 24-hour
usage of any team, and in each seed 1 or 2 more speech jobs wait over 10
minutes under fair than under fifo.)* Its median p90 is 13.1 under both,
though fair is higher in 3 seeds (`0 / 2 / 3`).

**Not explained by the fairness rule.** Forecast, the team with the fewest
GPU-hours (166.8), has a p90 of 10.0 under fair vs 8.0 under fifo and is
never lower under fair (`0 / 2 / 3`). The usage rule would favour forecast,
so this is more likely the per-launch draws described above; it is small and
has not been traced further.

**Who is disadvantaged.** Vision, under both policies: only 36.1% (fair)
and 36.5% (fifo) of its jobs start within 10 minutes, against 87.5% or more
for every other team, and its p90 is 143.1 vs 136.3 min. That is the burst
(next section), not the policy. Beyond that: platform under fifo, speech
slightly under fair.

### the burst window

**The burst is one team, and the node cap is what limits it.** The window
holds vision 178, platform 3 and speech 3 rows (§1); 176 jobs remain after
duplicates. They arrive almost at once: the queue is 0 (0–4) at
2025-03-05T06:00Z and 150 (149–156) at 2025-03-05T06:15Z, and the peak, 151
(149–157), comes 80.0 (75.0–85.0) min into the window under fair and 85.0
(75.0–85.0) under fifo. All teams together hit the cap of 10 live nodes
(peak nodes 10, §2). *(Checked on the replay databases, seed 42: 163 of the
176 burst jobs ask for 1 GPU; at 06:30Z both policies run 3 gpu-l, 5 gpu-m
and 2 gpu-s nodes, i.e. 46 GPUs, where 10 gpu-l nodes would hold 80.)*

**So the backlog drains at the rate the capacity allows, under either
order.** At the window end (2025-03-05T08:00Z) the queue is still 65 (28–89)
under fair and 58 (44–89) under fifo; the last burst job starts +82.8
(+29.4–+105.5) min after the window ends under fair and +69.2 (+55.8–+96.0)
under fifo, but fair is later in only 3 seeds (`1 / 1 / 3`). The median
queue is back to 0 by 2025-03-05T09:30Z under fair and 2025-03-05T09:15Z
under fifo. Queue order cannot make the drain faster; it only decides who
else waits behind it. Fair lets platform's burst jobs through (recovery -37.0
vs +53.7 min, `4 / 1 / 0`), while speech's 3 burst jobs wait about as long
under both (burst wait p90 103.2 vs 95.0, `1 / 2 / 2`) because speech's high
usage puts it behind under fair too. Vision's own recovery is +69.3 vs +56.4
min (`1 / 1 / 3`): slightly later under fair, not consistently.

### idle timeout: cost vs wait

**A shorter timeout is cheaper in every seed, and no wait penalty is
visible.** With fair, the grand total is $2,965.00 at 2 minutes, $3,167.67 at
10 and $3,348.16 at 30, and the ranges do not overlap ($2,867.92–$3,017.54,
$3,062.65–$3,185.46, $3,279.33–$3,421.28), so every seed is cheaper at 2 than
any seed at 10. Idle share falls with it: 23.3%, 28.2%, 32.1%. Wait p90 for
all teams stays at 123.8, 122.7 and 123.5 min, with ranges that overlap
(83.9–145.9, 91.8–156.6, 117.7–131.7).

**Why the wait barely moves.** Two rules limit what a short timeout can
take away: a ready node lives at least 10 minutes before it can be reaped
(D1), and a node that a queued job of its team fits on is never reaped (D5).
So a shorter timeout only reaps nodes nobody is waiting for; the price is
more launches (208 nodes at 2 minutes, 179 at 10, 145 at 30), each with a
4–8 minute boot for the next job that needs one, a chance of never becoming
ready, and the 10-minute minimum bill.

**What this table cannot show.** The sweep reports only the all-team p90.
The share of jobs started within 10 minutes and per-team waits at each
timeout are in `replay_results.json` but not in a table, and the extra
never-ready overhead from more launches is not broken out. The p90 is
dominated by the burst backlog, which the timeout does not touch, so a
change in ordinary waits could hide behind it.

### what we would change

In order of evidence; each needs a replay before it is adopted.

1. **Lower the default idle timeout from 10 to 2–5 minutes.** It is the
   only change here that is better in every seed ($2,965.00 vs $3,167.67)
   with no visible wait cost. Before adopting it, add the
   started-within-10-minutes share and per-team p50 to the sweep table, and
   check them at 2 and 5 minutes: the overall p90 alone could hide a slower
   start for ordinary jobs.
2. **Keep `fair` as the default.** It has no consistent cost difference
   (`3 / 0 / 2`), it protects a light team that arrives during someone
   else's burst (platform `4 / 1 / 0`), and its one consistent cost is small
   and falls on the heaviest user (speech 87.5% vs 89.3%). If speech's loss
   matters, shortening the 24-hour usage window or capping how far usage can
   push a team back are the knobs to try.
3. **Treat bursts as a capacity problem, not an ordering problem.** At the
   cap, the run holds 46 GPUs where 10 gpu-l nodes would hold 80, and the
   burst is mostly 1-GPU jobs. A rule worth testing: while a team has a long
   queue at the cap, launch (or replace idle small nodes with) gpu-l for it,
   which costs $8.50 per hour for 8 GPUs, about the price of eight gpu-s at
   $1.00 each, in one of the 10 slots. The current fallback limit of 2.0
   times the preferred price stops a 1-GPU launch from ever moving up a
   size (gpu-m costs $4.50), so this needs its own rule. Interruptible nodes (35% of
   the price, not used) would lower the cost of burst capacity but not add
   capacity, since they count towards the 10-node cap.
4. **Measure more before changing more.** 5 seeds of one week leave most
   fair-vs-fifo gaps inside seed noise. More seeds, a second trace, and the
   missing sweep metrics above would turn several of these "no consistent
   difference" results into answers.
