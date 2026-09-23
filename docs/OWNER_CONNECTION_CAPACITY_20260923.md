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

Pending deployment and live readback. Do not cite this section as acceptance
until it is updated with the release ID, test results and recovery evidence.
