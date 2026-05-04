"""SQLite upsert deduplication by URL."""

import os
import tempfile

from mufti_scraper.db.repository import FatwaRepository


def test_upsert_skips_duplicate_url():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        url = f"sqlite:///{path.replace(os.sep, '/')}"
        repo = FatwaRepository(url)
        s = repo.new_session()
        try:
            assert (
                repo.upsert_fatwa(
                    s,
                    question="Q",
                    answer="A",
                    source="T",
                    url="https://example.com/f/1",
                    category=None,
                    date=None,
                )
                == "inserted"
            )
            s.commit()
            assert (
                repo.upsert_fatwa(
                    s,
                    question="Q2",
                    answer="A2",
                    source="T",
                    url="https://example.com/f/1",
                    category=None,
                    date=None,
                )
                == "skipped"
            )
            s.commit()
        finally:
            s.close()
            repo.engine.dispose()
    finally:
        try:
            os.unlink(path)
        except PermissionError:
            pass
