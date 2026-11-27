# Replay results

Trace `01a_trace.csv`, replayed through the HTTP API on the simulated clock. Seeds: 42, 43, 44, 45, 46. Burst window: [2025-03-05T05:00:00Z, 2025-03-05T08:00:00Z).

Method: percentiles are nearest-rank: sort the n values, take the value at rank ceil(p/100 * n) (1-based); no interpolation, so every percentile is an observed wait. Wait = started_at − submitted_at. Each cell is the median across seeds (the lower median for an even count) with the min–max range in brackets when the seeds differ. Every number is in `replay_results.json` (money in micro-dollars).

## 1. Data quality

Full report: [data_quality.md](data_quality.md).

```text
rows 494: 492 accepted, 2 rejected
  rejected: gpus not in 1, 2, 4, 8 (lines 88, 393)
non-UTC offsets converted: 15
out-of-order rows: 23
duplicate rows: 14 (1 only after UTC conversion); collapse into one job via idempotency
time range: 2025-03-03T01:57:00Z .. 2025-03-09T16:33:00Z
rows per team: forecast 64, nlp 83, platform 25, speech 56, vision 264
in burst window [2025-03-05T05:00:00Z, 2025-03-05T08:00:00Z): platform 3, speech 3, vision 178
actual > 4 x estimate: 5 rows
```

## 2. Per team (median across seeds)

Each cell is that number's median across seeds, taken on its own, so team rows need not add up to the **all** row. Per-seed values are in `replay_results.json`.

### fair@10

| team | jobs | wait p50 (min) | p90 | p99 | max | started ≤ 10 min | GPU-h | busy | idle | total | idle share | nodes | never ready | capacity refusals | peak queue | peak nodes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| forecast | 63 | 3.0 | 10.0 | 22.5 | 22.5 | 88.9% | 166.8 | $180.68 | $108.52 | $287.44 | 37.4% | 30 | 1 | 1 | 2 | 5 |
| nlp | 80 | 5.4 | 8.7 | 49.5 | 49.5 | 90.0% | 365.6 | $398.91 | $205.81 | $604.71 | 34.0% | 50 | 1 | 10 | 3 | 7 |
| platform | 24 | 0.0 | 8.0 | 29.4 | 29.4 | 91.7% | 192.8 | $216.73 | $180.74 | $397.48 | 45.5% | 10 | 0 | 0 | 2 | 2 |
| speech | 56 | 5.5 | 13.1 | 103.2 | 103.2 | 87.5% | 985.6 | $1,049.58 | $119.47 | $1,168.44 | 10.2% | 43 | 1 | 8 | 3 | 7 |
| vision | 255 | 52.3 | 143.1 | 158.2 | 164.3 | 36.1% | 381.4 | $419.84 | $269.79 | $689.63 | 39.1% | 51 | 2 | 6 | 151 | 9 |
| **all** | 478 | 6.8 | 122.7 | 157.3 | 164.3 | 60.9% | 2,092.4 | $2,263.78 | $892.05 | $3,153.54 | 28.2% | 179 | 3 | 38 | 151 | 10 |

Provider overhead (never-ready nodes): $3.50. Grand total: $3,167.67.

### fifo@10

| team | jobs | wait p50 (min) | p90 | p99 | max | started ≤ 10 min | GPU-h | busy | idle | total | idle share | nodes | never ready | capacity refusals | peak queue | peak nodes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| forecast | 63 | 0.6 | 8.0 | 23.3 | 23.3 | 90.5% | 166.8 | $182.00 | $104.28 | $286.39 | 36.4% | 27 | 0 | 1 | 2 | 4 |
| nlp | 80 | 5.1 | 9.6 | 36.6 | 36.6 | 90.0% | 365.6 | $399.58 | $205.92 | $604.83 | 34.0% | 52 | 1 | 11 | 3 | 7 |
| platform | 24 | 0.0 | 90.7 | 117.7 | 117.7 | 87.5% | 192.8 | $216.73 | $180.69 | $397.42 | 45.5% | 10 | 0 | 0 | 3 | 2 |
| speech | 56 | 5.7 | 13.1 | 95.0 | 95.0 | 89.3% | 985.6 | $1,049.37 | $117.15 | $1,167.04 | 10.0% | 43 | 1 | 8 | 3 | 7 |
| vision | 255 | 44.5 | 136.3 | 151.9 | 153.5 | 36.5% | 381.4 | $419.69 | $256.28 | $676.51 | 37.9% | 52 | 2 | 6 | 151 | 10 |
| **all** | 478 | 6.5 | 116.8 | 150.6 | 153.5 | 61.3% | 2,092.4 | $2,266.19 | $852.71 | $3,117.12 | 27.2% | 181 | 3 | 39 | 151 | 10 |

Provider overhead (never-ready nodes): $3.38. Grand total: $3,130.37.

## 3. Fair vs fifo

Both at idle timeout 10 min. Last column: in how many of the 5 seeds fair is lower / equal / higher than fifo, comparing the same seed.

| metric | fair | fifo | seeds fair < / = / > fifo |
|---|---|---|---|
| grand total | $3,167.67 ($3,062.65–$3,185.46) | $3,130.37 ($3,066.40–$3,183.60) | 3 / 0 / 2 |
| idle share | 28.2% (25.9%–28.6%) | 27.2% (25.9%–28.7%) | 3 / 0 / 2 |
| started ≤ 10 min, all | 60.9% (58.8%–61.9%) | 61.3% (60.3%–61.7%) | 2 / 2 / 1 |
| wait p90 (min), all | 122.7 (91.8–156.6) | 116.8 (98.6–152.3) | 1 / 1 / 3 |
| wait p90 (min), forecast | 10.0 (8.0–15.4) | 8.0 (7.7–10.0) | 0 / 2 / 3 |
| wait p90 (min), nlp | 8.7 (7.9–19.1) | 9.6 (7.9–11.0) | 1 / 3 / 1 |
| wait p90 (min), platform | 8.0 (7.1–82.9) | 90.7 (15.4–133.0) | 4 / 1 / 0 |
| wait p90 (min), speech | 13.1 (12.3–35.9) | 13.1 (8.4–35.9) | 0 / 2 / 3 |
| wait p90 (min), vision | 143.1 (105.6–176.2) | 136.3 (115.3–168.9) | 1 / 1 / 3 |
| burst wait p90 (min), platform | 16.4 (12.2–109.9) | 117.7 (16.4–160.0) | 4 / 1 / 0 |
| burst wait p90 (min), speech | 103.2 (29.4–124.8) | 95.0 (29.4–112.4) | 1 / 2 / 2 |
| burst wait p90 (min), vision | 148.8 (116.2–187.1) | 139.2 (123.0–175.4) | 1 / 1 / 3 |
| started ≤ 10 min, forecast | 88.9% (81.0%–92.1%) | 90.5% (88.9%–95.2%) | 3 / 2 / 0 |
| started ≤ 10 min, nlp | 90.0% (85.0%–91.2%) | 90.0% (88.8%–91.2%) | 1 / 4 / 0 |
| started ≤ 10 min, platform | 91.7% (87.5%–91.7%) | 87.5% | 0 / 2 / 3 |
| started ≤ 10 min, speech | 87.5% (78.6%–89.3%) | 89.3% (80.4%–91.1%) | 5 / 0 / 0 |
| started ≤ 10 min, vision | 36.1% (34.5%–40.8%) | 36.5% (34.1%–37.6%) | 1 / 1 / 3 |

## 4. Idle timeout sweep

Policy fair.

| idle minutes | grand total | idle share | wait p90 (min), all | nodes launched |
|---|---|---|---|---|
| 2 | $2,965.00 ($2,867.92–$3,017.54) | 23.3% (20.7%–24.7%) | 123.8 (83.9–145.9) | 208 (203–212) |
| 10 | $3,167.67 ($3,062.65–$3,185.46) | 28.2% (25.9%–28.6%) | 122.7 (91.8–156.6) | 179 (177–194) |
| 30 | $3,348.16 ($3,279.33–$3,421.28) | 32.1% (30.7%–33.5%) | 123.5 (117.7–131.7) | 145 (142–161) |

## 5. Burst window

Window [2025-03-05T05:00:00Z, 2025-03-05T08:00:00Z), idle timeout 10 min. Queue = jobs submitted and not yet started, at that instant. Peak: the largest queue at any instant in the window, and how long after its start it was first reached. Recovery: when the last job submitted in the window started, minus the window end (negative: before it ended). Median (min–max) across seeds.

| metric | fair@10 | fifo@10 | seeds fair < / = / > fifo |
|---|---|---|---|
| jobs submitted in the window | 176 | 176 | 0 / 5 / 0 |
| peak queue in the window | 151 (149–157) | 151 (150–157) | 1 / 4 / 0 |
| peak reached after (min) | 80.0 (75.0–85.0) | 85.0 (75.0–85.0) | 1 / 4 / 0 |
| recovery (min) | +82.8 (+29.4–+105.5) | +69.2 (+55.8–+96.0) | 1 / 1 / 3 |
| recovery (min), platform | -37.0 (-37.0–+45.9) | +53.7 (-37.0–+96.0) | 4 / 1 / 0 |
| recovery (min), speech | +69.2 (-33.6–+90.8) | +61.0 (-33.6–+78.4) | 1 / 2 / 2 |
| recovery (min), vision | +69.3 (+24.3–+105.5) | +56.4 (+33.6–+85.8) | 1 / 1 / 3 |

Queue length, all teams (per team in `replay_results.json`):

| time (UTC) | fair@10 | fifo@10 |
|---|---|---|
| 2025-03-05T05:00Z | 3 | 3 |
| 2025-03-05T05:15Z | 3 (1–3) | 3 (1–3) |
| 2025-03-05T05:30Z | 0 (0–1) | 0 (0–1) |
| 2025-03-05T05:45Z | 1 (1–4) | 1 (1–4) |
| 2025-03-05T06:00Z | 0 (0–4) | 0 (0–4) |
| 2025-03-05T06:15Z | 150 (149–156) | 150 (148–156) |
| 2025-03-05T06:30Z | 150 (142–154) | 150 (149–154) |
| 2025-03-05T06:45Z | 136 (123–144) | 136 (131–144) |
| 2025-03-05T07:00Z | 132 (110–141) | 126 (125–141) |
| 2025-03-05T07:15Z | 114 (91–129) | 110 (101–129) |
| 2025-03-05T07:30Z | 97 (70–115) | 93 (86–115) |
| 2025-03-05T07:45Z | 83 (49–104) | 78 (65–104) |
| 2025-03-05T08:00Z | 65 (28–89) | 58 (44–89) |
| 2025-03-05T08:15Z | 51 (13–80) | 45 (28–80) |
| 2025-03-05T08:30Z | 35 (0–65) | 32 (6–65) |
| 2025-03-05T08:45Z | 20 (0–54) | 12 (4–54) |
| 2025-03-05T09:00Z | 3 (0–42) | 1 (0–37) |
| 2025-03-05T09:15Z | 1 (0–25) | 0 (0–17) |
| 2025-03-05T09:30Z | 0 (0–8) | 0 (0–3) |
| 2025-03-05T09:45Z | 0 (0–2) | 0 (0–1) |
| 2025-03-05T10:00Z | 0 | 0 |

## Analysis

- TODO: fair vs fifo
- TODO: the burst window
- TODO: idle timeout: cost vs wait
- TODO: what we would change
