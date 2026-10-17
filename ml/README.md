# ml/ - training, benchmarking and the real-data path

Everything here runs offline and never imports the backend (the one exception is the dev-only
`scripts/benchmark.py`, which loads the shipped rule classifier by file path). Feature code is
shared with the backend via `libs/aquaguard_features`.

```
python -m ml.scripts.generate_data  [--profile default|shifted] [--sensor-model none|yfs201] [--yfs-min-flow 1.0]
python -m ml.scripts.train_detector        # models + ml/reports/detector_report.md
python -m ml.scripts.train_forecaster      # ml/reports/forecaster_report.md
python -m ml.scripts.benchmark             # ml/reports/benchmark.md (+ benchmark_results.json), ~15 min
python -m ml.scripts.train_from_export ... # real-data path (below)
python -m pytest ml/tests -q
```

## Honest status

All reported numbers are from the **synthetic** generator. `ml/reports/benchmark.md` compares the
ML detector with simple rules on identical splits/metrics with bootstrap CIs and paired tests, and
states plainly where an advantage is **not demonstrated**. It also stress-tests distribution shift
and an *assumed* YF-S201 sensor model (still synthetic) and reports the smallest leak the
pipeline can see with that sensor. Do not quote metrics without that report's caveats.

### Benchmark flags (deterministic: every random stream derives from `--seed`)

| flag | meaning |
|---|---|
| `--flats N --days D --seed S` | cohort (default 40 x 60, seed 42) |
| `--boot B` | bootstrap resamples (default 2000; use >= 1000) |
| `--debounce K` | K consecutive flagged windows for the debounced metrics (default 3) |
| `--quick` | smaller RandomForest |
| `--skip-shift`, `--skip-curve` | skip the shift matrix / minimum-detectable-leak curve |
| `--out-dir DIR` | where `benchmark.md` and `benchmark_results.json` go |

A suggested Makefile target (not wired): `benchmark: ; python -m ml.scripts.benchmark`.

## Real-data path: `train_from_export.py`

Export two files from the production database (CSV or Parquet; timestamps UTC).

`readings.csv` header (one row per sensor reading; any sampling rate, it is resampled to per-minute means):

```
flat_id,ts,flow_lpm
```

`feedback.csv` header (one row per anomaly; `fault_start`/`resolved_at` may be empty):

```
alert_id,flat_id,anomaly_type,detected_at,resolved_at,fault_start,outcome
```

`outcome`: `confirmed` (plumber confirmed: gold positive, weight 1), `acknowledged` (operator accepted:
weak positive, 0.5), `overridden` (operator rejected: negative, 0.5), `false_alarm` (negative, 1.0),
anything else = unlabeled. The database has no plumber-confirmation field; join it from the
maintenance/ticket system into `outcome`/`fault_start`.

Export SQL (PostgreSQL; adjust the window):

```sql
\copy (
  SELECT r.flat_id::text AS flat_id,
         to_char(r.timestamp AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS ts,
         r.flow_rate AS flow_lpm
  FROM flow_readings r
  WHERE r.timestamp >= '2026-01-01' AND r.timestamp < '2026-07-01'
  ORDER BY r.flat_id, r.timestamp
) TO 'readings.csv' CSV HEADER

\copy (
  SELECT al.id::text AS alert_id, an.flat_id::text AS flat_id, an.anomaly_type,
         to_char(an.detected_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS detected_at,
         to_char(an.resolved_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS resolved_at,
         NULL AS fault_start,                       -- from the plumber ticket, if you have one
         CASE WHEN al.state = 'overridden'        THEN 'overridden'
              WHEN al.acknowledged_at IS NOT NULL THEN 'acknowledged'
              ELSE 'unlabeled' END AS outcome        -- overwrite with 'confirmed' from tickets
  FROM anomalies an
  LEFT JOIN LATERAL (SELECT * FROM alerts WHERE anomaly_id = an.id
                     ORDER BY created_at DESC LIMIT 1) al ON true
  WHERE an.detected_at >= '2026-01-01' AND an.detected_at < '2026-07-01'
) TO 'feedback.csv' CSV HEADER
```

Run:

```
python -m ml.scripts.train_from_export --readings readings.csv --feedback feedback.csv \
    --model-dir ml/models --tz Europe/Berlin            # trains + guards, writes a CANDIDATE only
python -m ml.scripts.train_from_export ... --promote     # overwrite live models if the guard passes
```

* **Unsupervised part**: IsolationForest fitted on windows not near any alert (train period).
* **Supervised part**: RandomForest only if there are enough labelled episodes in both the train and the
  held-out period (`--min-positive-episodes`, `--min-holdout-positives`); otherwise the incumbent
  classifier is copied unchanged and the reason is recorded in the metadata.
* **Guard**: candidate and incumbent are scored on the same held-out (last `--holdout-frac`)
  windows; every comparable metric (`iso_fp_rate` lower-is-better; `iso_recall`, `clf_anomaly_f1`,
  `clf_macro_f1` higher-is-better) must be not worse (within `--tolerance`, default 0). The
  incumbent is only unpickled if its SHA-256 matches `manifest.json`; a mismatch aborts (exit 2).
  Live files are **never** overwritten without `--promote` and a passing guard (exit 3 when a
  requested promotion was refused). The candidate is always kept in `<model-dir>/candidates/<utc>/`
  with the same manifest/metadata layout as `train_detector`.
* **Shadow mode before promotion**: point a second `ModelStore` at the candidate directory (or replay
  the export through both models), log both decisions without alerting from the candidate, and compare
  against operator outcomes for 2-4 weeks. Promote only if precision (vs `overridden`) and recall
  (vs `confirmed`) are not worse on the same live windows. The offline guard is necessary, not sufficient:
  labels are operator feedback (selection-biased - only alerts that fired get a label; un-alerted
  windows are *assumed* normal).
