# Owner database and async-read capacity — 2026-09-23

## What the numbers mean

`QUANT_ASYNC_READ_POOL_MAX_SIZE=16` and PostgreSQL `max_connections=50` were
application/operator settings, not hardware limits. The latter was introduced
at the 2026-09-01 platform initialization without a recorded load-test basis.
The former was a hardcoded clamp, so a larger environment value was silently
ignored. Neither number by itself described sustainable request concurrency.

PostgreSQL reserves three of its connection slots for superusers. The owner
API also has a separate synchronous pool, while two peer services and scheduled
jobs share the same cluster. Pool maxima are per process, not a global budget.

## Isolated machine test (completed)

The test used the installed PostgreSQL 16.15 binary on the same Windows host,
an isolated scratch cluster on F:, `shared_buffers=4GB`, loopback port 55631,
and a scale-1 `pgbench` dataset. `pgbench -S` ran its built-in read-only,
cache-friendly account lookup for 10 seconds per step. No production table was
written. The production owner API remained healthy throughout. The scratch
server was stopped after the test; its evidence directory is
`G:\StockPlatform\logs\runtime\acceptance\pg-capacity-20260923`.

| `max_connections` | Clients | Failed transactions | TPS excluding connect | Mean query latency | Initial connect time |
|---:|---:|---:|---:|---:|---:|
| 128 | 32 | 0 | 194,501 | 0.165 ms | 1.00 s |
| 128 | 64 | 0 | 235,735 | 0.271 ms | 1.60 s |
| 128 | 96 | 0 | 231,995 | 0.414 ms | 2.30 s |
| 128 | 120 | 0 | 237,211 | 0.506 ms | 2.82 s |
| 256 | 160 | 0 | 219,351 | 0.729 ms | 3.98 s |
| 256 | 224 | 0 | 231,971 | 0.966 ms | 5.23 s |

This proves the machine can run **at least 224 PostgreSQL clients** in this
lightweight isolated workload while another 4 GB-shared-buffer production
cluster is online. It does **not** establish an absolute crash threshold or
claim that 224 production-heavy queries would be safe. Throughput flattened
around 64 clients; additional sessions mostly increased latency and connect
time. Deliberately driving a shared workstation until the OS or database
crashes would not yield a useful operating budget.

## Production workload evidence before the change

On the owner API with an 8-slot async pool, 20 simultaneous replay-readiness
GETs all returned 200 (P95 13.95 s), while 24 returned 23×200 and 1×500.
The exception was `psycopg_pool.PoolTimeout` after 10 seconds waiting for a
connection. A temporary 16-slot owner pool returned 16×200 (P95 6.57 s), but
cluster sessions reached 49/50. Across that bounded experiment, PostgreSQL
logged 11.72 GB of cumulative temporary files and one Windows shared-memory
reservation error; peer queries were slow in the same window. Temporal
coincidence is not proof of which request caused the Windows error. The
temporary setting was removed and the original runtime configuration was
restored exactly.

The replay-readiness route is a high-cost full-history query. Its result does
not define the capacity of ordinary short reads. It is also not an acceptable
reason to simply increase every connection pool: more parallel spills can
degrade the peer.

## Operating target and acceptance

The planned production budget is PostgreSQL `max_connections=100` and owner
`QUANT_ASYNC_READ_POOL_MAX_SIZE=20`. These are **chosen operating settings**, not
new claims of hard limits. The database value leaves room for the owner sync
pool (12), owner async pool (20), two peer sync/async pools (up to 12+8 each),
and auxiliary jobs. Actual use, not merely the sum of configured maxima, must
be measured after activation. The application no longer silently clamps an
operator-provided async max to 16.

Production acceptance remains separate from the isolated test: verify the
running PostgreSQL value, owner pool readback, local and remote health, peak
session count, error logs, and representative read latency. A 20-way full-
history replay stampede is not an acceptance workload; that query must be
optimized or coalesced before its own concurrency is raised. Restore the
prior PostgreSQL config and runtime pool value if startup, peer recovery,
connection headroom, or error rates regress.

## Production result

Release `20260923T184812-4635adb9a39a-clean` was published from commit
`4635adb9a39ae173fa2b71acbc15fc5129a8feb1` after the normal release gates
(3372 backend tests passed, 130 skipped; 145 frontend tests passed; typecheck,
build and Windows storage-wiring checks passed). The managed PostgreSQL config
was backed up in the private `G:\StockPlatform\config` directory before a
controlled after-close restart. Live SQL readback returned
`max_connections=100`; the owner `/health` endpoint returned `status=ok`,
`async_database_pool.min_size=2` and `max_size=20` after its targeted restart.

The production `/api/v1/data-readiness/features` route uses the native async
read pool. Three waves of 20, 40 and 20 simultaneous GETs returned 80/80 HTTP
200. Their P95 latencies were 401, 437 and 261 ms respectively; the 40-way
wave grew the observed pool to 13 without a waiter. A separate 24-client
read-only `pg_sleep(1.5)` probe on the running production database moved
observed sessions from 49 to a peak of 73, completed 24/24, and returned to
49 afterward. This proves the live cluster accepts more than the former 50
configured slots; it does not authorize 73 expensive queries as a normal load.

An additional **100-way burst did not pass**: 78 HTTP 200 and 22 HTTP 500.
The pool reached its configured 20 connections, while its separate hardcoded
`max_waiting=64` guard raised `psycopg_pool.TooManyRequests` for the excess
arrivals. The failure was not PostgreSQL's 100-connection setting. A follow-up
40-way wave returned 40/40 HTTP 200 (P95 272 ms), and the owner remained
healthy. Consequently, this release is accepted for the requested 20 active
async database lanes and measured 40-way short-read bursts, **not** for
100-way simultaneous arrival or unbounded queuing. The `TooManyRequests` to
HTTP 500 mapping should be improved separately if that arrival rate becomes a
supported workload.

The shared-runtime verifier returned `status=verified` after the restart:
remote owner and peer API checks were both HTTP 200, with all three reverse
tunnel ports present. It also printed one transient `curl` 10-second timeout
while completing its other probes, so the peer path has not been certified
latency-free. A final verifier run after the 100-way burst returned
`status=verified` and both remote APIs HTTP 200 without that timeout.
PostgreSQL logged no connection-slot exhaustion or temporary
files during the acceptance window. It did continue to log Windows shared-
memory reservation error 487 twice after the restart; the same log contained
75 such events before the restart while the cap was still 50. That pre-existing
host-level issue is not evidence that the new cap caused it and deserves its
own investigation. A peer scheduler's recurring missing `known_at` column
error is likewise outside this connection-capacity change.

The prior managed PostgreSQL config and exact pre-change owner runtime env
remain in private `G:\StockPlatform\config` as rollback materials. Neither
contains evidence suitable for source control because the env file includes
secrets.
