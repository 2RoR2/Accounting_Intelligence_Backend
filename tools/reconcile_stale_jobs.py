"""Mark orphaned jobs from previous environments as failed, without deleting data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.connection import connect


def main() -> None:
    with connect() as db:
        rows = db.execute(
            """
            SELECT j.id, j.document_id, d.storage_path
            FROM pending_jobs AS j
            JOIN documents AS d ON d.id = j.document_id
            WHERE j.status IN ('pending', 'processing')
            """
        ).fetchall()
        stale = [
            row for row in rows
            if row["storage_path"].startswith(("D:\\", "/opt/render/", "mock/"))
            or not Path(row["storage_path"]).is_file()
        ]
        job_ids = [row["id"] for row in stale]
        document_ids = [row["document_id"] for row in stale]
        if job_ids:
            db.execute(
                """
                UPDATE pending_jobs
                SET status = 'failed',
                    last_error = 'STALE_INPUT_PATH: source file is no longer available; upload the document again.',
                    finished_at = now()
                WHERE id = ANY(%s::uuid[])
                """,
                (job_ids,),
            )
        if document_ids:
            db.execute(
                """
                UPDATE documents
                SET status = 'failed'
                WHERE id = ANY(%s::uuid[])
                  AND status IN ('queued', 'processing')
                """,
                (document_ids,),
            )
        print(f"reconciled {len(document_ids)} stale jobs")


if __name__ == "__main__":
    main()
