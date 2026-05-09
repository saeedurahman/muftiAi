"""CLI: run selected sources, populate DB, optional PDF folder."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mufti_scraper.config import ScraperConfig
from mufti_scraper.db.repository import FatwaRepository
from mufti_scraper.http_client import PoliteHttpClient
from mufti_scraper.pdf_extract import iter_fatwas_from_pdf_dir
from mufti_scraper.robots import RobotsCache
from mufti_scraper.sources.registry import all_source_names, get_sources

logger = logging.getLogger("mufti_scraper")


def _setup_logging(config: ScraperConfig) -> None:
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / config.log_file
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = RotatingFileHandler(
        log_path,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(fh)
    root.addHandler(ch)


def run_scraper(
    config: ScraperConfig,
    source_names: list[str],
    limit: int | None,
    dry_run: bool,
) -> dict[str, int]:
    repo = FatwaRepository(config.database_url)
    client = PoliteHttpClient(config)
    robots = RobotsCache(config.user_agent, client=client)
    sources = get_sources(source_names, config)

    stats = {"inserted": 0, "skipped": 0, "failed": 0, "parsed_none": 0}
    batch = 0
    session = repo.new_session()
    try:
        for src in sources:
            logger.info("Collecting URLs for source %s", src.name)
            try:
                urls = src.iter_detail_urls(client, robots, limit)
            except Exception as e:
                logger.exception("URL discovery failed for %s: %s", src.name, e)
                repo.log_error(session, "", src.name, f"discovery: {e}")
                continue
            logger.info("Source %s: %d URLs to fetch", src.name, len(urls))

            for url in urls:
                if not robots.can_fetch(url):
                    logger.info("Skipping (robots): %s", url)
                    continue
                try:
                    if getattr(src, "has_custom_fetcher", False):
                        html = src.fetch_page(url)
                    else:
                        r = client.get(url)
                        r.raise_for_status()
                        encoding = r.encoding or r.apparent_encoding or "utf-8"
                        html = r.content.decode(encoding, errors="replace")
                except Exception as e:
                    stats["failed"] += 1
                    repo.log_error(session, url, src.name, f"fetch: {e}")
                    continue

                parsed = src.parse_page(html, url)
                if not parsed:
                    stats["parsed_none"] += 1
                    repo.log_error(session, url, src.name, "parse returned empty")
                    continue

                if dry_run:
                    logger.info("[dry-run] would store %s", parsed.url[:80])
                    stats["inserted"] += 1
                    continue

                status = repo.upsert_fatwa(
                    session,
                    question=parsed.question,
                    answer=parsed.answer,
                    source=parsed.source,
                    url=parsed.url,
                    category=parsed.category,
                    date=parsed.date,
                )
                stats[status] = stats.get(status, 0) + 1
                batch += 1
                if batch >= config.batch_commit_size:
                    session.commit()
                    batch = 0
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        for src in sources:
            closer = getattr(src, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as e:
                    logger.debug("Source close failed for %s: %s", src.name, e)
        session.close()

    return stats


def run_pdf(
    config: ScraperConfig,
    pdf_dir: Path,
    source_label: str,
    dry_run: bool,
) -> dict[str, int]:
    repo = FatwaRepository(config.database_url)
    items = iter_fatwas_from_pdf_dir(pdf_dir, source_label)
    stats = {"inserted": 0, "skipped": 0, "failed": 0}
    session = repo.new_session()
    try:
        for parsed in items:
            if dry_run:
                stats["inserted"] += 1
                continue
            try:
                status = repo.upsert_fatwa(
                    session,
                    question=parsed.question,
                    answer=parsed.answer,
                    source=parsed.source,
                    url=parsed.url,
                    category=parsed.category,
                    date=parsed.date,
                )
                stats[status] = stats.get(status, 0) + 1
            except Exception as e:
                stats["failed"] += 1
                repo.log_error(session, parsed.url, parsed.source, str(e))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Polite multi-source fatwa scraper (educational indexing).",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default=",".join(all_source_names()),
        help="Comma-separated: banuri,almuftionline,deoband,karachi,alikhlas",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max items per source")
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="SQLAlchemy URL (default sqlite:///fatawa.db or MUFTI_DATABASE_URL)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fetch/parse but do not write DB")
    parser.add_argument(
        "--pdf-dir",
        type=str,
        default=None,
        help="If set, ingest PDFs from this directory (after web sources unless --pdf-only).",
    )
    parser.add_argument(
        "--pdf-source-name",
        type=str,
        default="Local PDF archive",
        help="Source label for PDF rows",
    )
    parser.add_argument(
        "--pdf-only",
        action="store_true",
        help="Only run PDF ingestion (skip web sources)",
    )
    args = parser.parse_args(argv)

    config = ScraperConfig.from_env()
    if args.db:
        config.database_url = args.db
    _setup_logging(config)

    total_stats: dict[str, int] = {}
    if not args.pdf_only:
        names = [x.strip() for x in args.sources.split(",") if x.strip()]
        logger.info("Starting web scrape: %s", names)
        total_stats = run_scraper(config, names, args.limit, args.dry_run)
        logger.info("Web scrape stats: %s", total_stats)

    if args.pdf_dir:
        p = Path(args.pdf_dir)
        if not p.is_dir():
            logger.error("PDF directory does not exist: %s", p)
            return 2
        logger.info("Ingesting PDFs from %s", p)
        pdf_stats = run_pdf(config, p, args.pdf_source_name, args.dry_run)
        logger.info("PDF stats: %s", pdf_stats)
        for k, v in pdf_stats.items():
            total_stats[k] = total_stats.get(k, 0) + v

    if not args.pdf_dir and args.pdf_only:
        logger.error("--pdf-only requires --pdf-dir")
        return 2

    print("Done.", total_stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
