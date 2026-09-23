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

## 2026-09-23 follow-up: highest accepted owner setting

The 20-lane acceptance above was not a maximum-capacity test. The owner was
subsequently tested in 24, 32, 40, 44, 46, 47, 48, 56, 60 and 64 lane stages,
with a restart and live `/health` readback for each configured stage. The
production version is now `20260923T195848-03e2ddf5db0b-clean`. It includes
the cold-start queue fix from `2d169ee8485b5dd2bec60cdf641c848d9efeeaba`:
the bounded async wait queue defaults to 128 and can be changed with
`QUANT_ASYNC_READ_POOL_MAX_WAITING`. The separate PostgreSQL-budget override
from `03e2ddf5db0b14a439aa2c589eed26324c0acc85` reads positive
`STOCK_PG_MAX_CONNECTIONS` from the adjacent private `runtime.env`; its
default remains 100. Both releases passed the normal publisher gates (3373
backend tests passed, 130 skipped; 145 frontend tests passed; typecheck and
build passed).

The final **deployed** private settings are `STOCK_PG_MAX_CONNECTIONS=128` and
`QUANT_ASYNC_READ_POOL_MAX_SIZE=47`; the queue uses its released default 128.
The production PostgreSQL restart was at 20:04 CST, followed by a targeted
owner API restart to rebuild connections. SQL returned 128. The final owner
health returned `status=ok`, async max/current 47/47, waiting 0; the adapter
returned `ok`. After the final 100-way mixed workload, 84 of 128 PostgreSQL
sessions were present (2 active), leaving 44 slots at that observation time.

Acceptance used two workloads rather than substituting a synthetic connection
count for application behavior:

- A mixed burst of short native-async reads (`features`, one market snapshot,
  20 level-1 rows, one Tushare raw row and `metrics`): the final two 100-request
  waves returned **200/200 HTTP 200**, P95 471 and 1144 ms. A 24-lane cold
  start originally returned 13/80 HTTP 500 from the old fixed 64-waiter guard;
  after the queue fix, the same 80-way cold burst returned 80/80 HTTP 200.
- A longer, read-only `history-estimate?universe_symbols=5500` query: at final
  max 47, two independent three-wave runs produced **282/282 HTTP 200**.
  The pool reached 47/47. Their warm-wave P95 values were about 3.3–3.8 s;
  `pg_stat_database.temp_bytes` increased by 0 in every measured wave. The
  licensed peer verifier ran concurrently in both runs and returned
  `status=verified`, remote owner/peer HTTP 200. In parallel, 32 extra owner
  `/health` probes all returned 200, slowest 3.43 s.

The important **failure boundary** is system-level, not the success of the
business query alone. At 48, 56, 60 and 64 lanes, the corresponding read
requests all returned 200, but a concurrent shared-runtime verification timed
out its owner `/health` request after 5 s in the strict cold/continuous-load
scenario. 40, 44, 46 and 47 passed that same scenario; 44, 46 and 47 each
passed twice (cold and warm). Therefore 47 is the **highest repeatedly
accepted setting in this defined workload**, not a physical ceiling or a
promise that every future workload can run 47 expensive SQL plans safely.
The health endpoint itself probes the shared async pool and then assembles a
synchronous database/resource payload; contention there is the observed
constraint. The full-history replay endpoint that previously spilled many GB
was deliberately not stampede-tested at 47.

Across the whole PostgreSQL-128 exploration there were nine Windows
shared-memory reservation (487) log events, all between 20:07 and 20:16 CST
during the higher-stage ramp. The error existed before this work; these events
cannot be attributed to one stage with certainty, but they are **not** hidden
as a clean log. Since the last such event, the final 47-lane acceptance had
zero connection-slot exhaustion and zero logged temporary-file events. The
peer scheduler's independent missing-`known_at` errors continue and are not
counted as an owner capacity success.

Rollback materials remain private and exact:

- `G:\StockPlatform\config\runtime.env.pre-owner-async-capacity-20260923T1934`
  is the earlier owner-20 environment.
- `G:\StockPlatform\config\runtime.env.pre-pg128-20260923T2004` and
  `G:\StockPlatform\config\postgresql-stock-platform.conf.pre-pg128-20260923T2004`
  are the paired owner-56/PostgreSQL-100 checkpoint before the 128 restart.

The safe rollback order is to restore the chosen private env/config pair,
restart PostgreSQL only if its configured max changes, then restart the owner
API and verify local and remote health. Do not copy these env backups into Git
or a release: they contain credentials. Changing only the owner pool max
requires just the targeted owner API restart.
