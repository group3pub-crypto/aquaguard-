# Ingest performance (measured)

All numbers below were measured on one machine with the real stack, no mocks:
Mosquitto 2.0.18 (auth + ACL), PostgreSQL 16.15 (fsync on, default-ish settings, local disk), the
FastAPI backend (one uvicorn worker, Python 3.13) with the trained ML detector loaded
(IsolationForest + RandomForest, 200 trees each), and `infra/loadtest/load_test.py` - **all on the
same 4 vCPU / 16 GB Intel Xeon 2.1 GHz VM**, so broker, database, backend and the load generator
compete for the same cores. A dedicated database/broker host would do better; a slower CPU worse.
Treat the figures as order-of-magnitude for this box, not as a capacity guarantee.

## Method

`python infra/loadtest/load_test.py --meters N --duration T --interval 1.0 --db-url ... --backend-pid PID`

* N simulated meters publish a QoS 1 flow reading every second (real devices send one per minute,
  so 500 meters at 1/s is a 60x overload of a 500-flat society).
* Meters are provisioned as flats (`--db-url` creates society `loadtest` + N flats if missing).
* "landed" = rows counted in `flow_readings`; throughput = landed / (publish window + drain time).
* Latency = time from `publish()` call to the row first being visible to a SQL reader polling every
  50 ms (resolution ~50 ms; publisher and reader share the clock because they run on one host).
* CPU = `utime+stime` of the backend process from `/proc` over the publish window (100% = one core).
* The ML detector runs inline per flat, debounced to once per 30 s (default), at most 2 at a time.

## Results

| Scenario | Offered | Landed | Throughput | Latency publish->row p50 / p95 | Backend CPU |
|---|---|---|---|---|---|
| **Before** (original code: per-message ORM transaction, 220 ms sklearn calls, detection awaited inline) 500 meters, 30 s | 478 msg/s | 3 879 / 14 830 (26 %) after a 60 s drain | 43 msg/s | 13.4 s / 53.7 s | 72 % |
| **After A** 500 meters, 30 s, detector effectively off (one initial run per flat) | 479 msg/s | 14 878 / 14 878 (100 %) | 479 msg/s | 0.31 s / 0.76 s | 88 % |
| **After B** 500 meters, 45 s, ML detection every 30 s per flat | 483 msg/s | 22 228 / 22 228 (100 %) | 483 msg/s | 0.18 s / 0.92 s | 86 % |
| **After C** 1 000 meters, 30 s (overload) | 931 msg/s | 27 986 / 28 690 (97.5 %), rest still draining after 30 s | ~460 msg/s sustained drain | 16 s / 26.5 s (queueing in the broker) | 64 % in window |

Reading the table: one backend process sustains roughly **480 msg/s on this VM** with ML detection
enabled. Above that the broker queues the surplus (persistent session, `max_queued_messages 50000`)
and latency grows; with the old queue limit of 5 000 the broker dropped ~26 % of an overload
burst (observed in an earlier run, not in the table). For realistic fleets (one reading per minute)
480 msg/s corresponds to roughly 28 000 flats per backend process *by arithmetic from this single
measurement*; that extrapolation was NOT measured (it ignores detection cost, dashboards and API load).

## What was slow, and what changed

1. **sklearn inference fanned out to joblib threads** (`n_jobs=-1` default inside the pickled
   models): ~220 ms per single-row prediction on 4 cores. Models are now loaded single-threaded
   (~15 ms RandomForest + ~19 ms IsolationForest, which also now needs one tree traversal instead of
   two).
2. **Per-message transaction and ORM unit of work** (~5 round trips + flat lookup join each).
   Replaced by a flat-id cache and a micro-batcher (`app/services/batcher.py`): one multi-row
   `INSERT .. ON CONFLICT DO NOTHING RETURNING` plus one sensor-health upsert per batch, committed
   before the MQTT ack.
3. **Single sequential consumer** that also awaited detection. Now bounded concurrent workers
   (`MQTT_WORKER_CONCURRENCY`, use 128 for load), manual acks after commit, and detections are
   limited to `DETECT_MAX_CONCURRENCY` (2); surplus triggers are shed (`detect_shed` counter) and
   retried on the flat's next reading. Per-flat jitter stops all flats re-triggering together.
4. **Broker queue** for the persistent backend session raised from 5 000 to 50 000 messages.

## Not measured / limits

* Single backend process only; MQTT 3.1.1 has no shared subscriptions, so scaling out needs MQTT 5
  shared subscriptions or sharded topics (not implemented).
* The remaining CPU is spread over ML inference (~35-40 % of samples under scenario B), paho/asyncio
  socket handling and SQL; PostgreSQL was not the bottleneck on this disk.
* No soak test (hours), no failure-injection under load, no TLS-listener load, no REST/WebSocket
  load concurrently with ingest.
