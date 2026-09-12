import threading

from app.db.connection import write_connection
from app.db.migrate import run_migrations


def test_concurrent_writes_do_not_lock(tmp_path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)

    n_threads = 8
    inserts_per_thread = 50
    errors: list[BaseException] = []

    def worker(thread_id: int) -> None:
        try:
            for i in range(inserts_per_thread):
                with write_connection(db_path) as conn:
                    conn.execute("BEGIN")
                    conn.execute(
                        """
                        INSERT INTO sources (name, interval_s, retention, enabled)
                        VALUES (?, 60, 'forever', 1)
                        """,
                        (f"source-{thread_id}-{i}",),
                    )
                    conn.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"writer thread(s) raised: {errors!r}"

    with write_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert count == n_threads * inserts_per_thread
