"""Read-only PG acceptance from a quant container's existing environment.

Set TEST_PGHOST to the candidate/production tunnel DNS name. Exercise independent
short queries alongside repeated 3 MB result transfers, using bounded timeouts.
Never write production tables or print credentials. Exit nonzero on any failure.
"""
import concurrent.futures
import json
import os
import statistics
import time

import psycopg


def query(large=False):
    start = time.monotonic()
    connection = None
    phase = 'connect'
    try:
        connection = psycopg.connect(host=os.environ.get('TEST_PGHOST', 'db-tunnel'),
                                     connect_timeout=3, options='-c statement_timeout=5000')
        phase = 'query'
        if large:
            rows = connection.execute("SELECT repeat('x',1000) FROM generate_series(1,3000)").fetchall()
            assert len(rows) == 3000 and all(len(row[0]) == 1000 for row in rows)
        else:
            assert connection.execute('SELECT 1').fetchone()[0] == 1
        elapsed = round(time.monotonic() - start, 3)
        return {'kind': '3MB' if large else 'small', 'ok': elapsed < (6 if large else 2), 'seconds': elapsed}
    except Exception as error:
        return {'kind': '3MB' if large else 'small', 'ok': False, 'phase': phase,
                'error': type(error).__name__, 'seconds': round(time.monotonic() - start, 3)}
    finally:
        if connection is not None:
            connection.close()


def main():
    batches = []
    for cycle in range(1, 4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(query, True) for _ in range(2)]
            futures += [executor.submit(query) for _ in range(16)]
            rows = [future.result() for future in futures]
        batch = {'cycle': cycle, 'passed': all(row['ok'] for row in rows), 'results': rows}
        batches.append(batch)
        print(json.dumps(batch), flush=True)
        time.sleep(1)
    small = [row['seconds'] for batch in batches for row in batch['results'] if row['kind'] == 'small']
    result = {'passed': all(batch['passed'] for batch in batches), 'small_queries': len(small),
              'large_queries': 6, 'small_max_seconds': max(small),
              'small_median_seconds': round(statistics.median(small), 3)}
    print(json.dumps(result), flush=True)
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
