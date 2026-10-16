import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from .client import ApiError, extract_rides
from .settings import Settings
from .timestamps import analyze


from .storage import save, as_json

def main():
    parser = argparse.ArgumentParser(description="Read-only Bafang proof of concept")
    parser.add_argument("--data-dir", default=os.getenv("DATA_DIR", "data"))
    parser.add_argument("--timezone", default=os.getenv("APP_TIMEZONE", "Europe/Zurich"))
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--size", type=int, default=20)
    parser.add_argument("--max-downloads", type=int, default=5)
    args = parser.parse_args()
    if args.start_page < 0 or args.max_pages < 1 or not 1 <= args.size <= 100 or args.max_downloads < 0:
        parser.error("Invalid pagination or download limit")
    os.umask(0o077)
    try:
        ZoneInfo(args.timezone)
        directory = Path(args.data_dir)
        client = Settings(data_dir=directory, app_timezone=args.timezone).client()
        seen, reports, pages, downloads = set(), [], 0, 0
        complete = False
        pagination = []
        for page in range(args.start_page, args.start_page + args.max_pages):
            payload = client.list_page(page, args.size)
            records = extract_rides(payload)
            save(directory / "pages" / f"page-{page}.json", as_json(payload))
            pages += 1
            metadata = payload.get("data", {}) if isinstance(payload, dict) else {}
            pagination.append({"requested_page": page, "requested_size": args.size,
                               "reported_page": metadata.get("current") if isinstance(metadata, dict) else None,
                               "reported_size": metadata.get("size") if isinstance(metadata, dict) else None,
                               "records": len(records)})
            if not records:
                complete = True
                break
            ids = {str(ride["id"]) for ride in records}
            if ids <= seen:
                raise ApiError("Pagination repeated a page; stopping safely")
            for ride in records:
                ride_id = str(ride["id"])
                if ride_id in seen:
                    continue
                seen.add(ride_id)
                filename = hashlib.sha256(ride_id.encode()).hexdigest()
                save(directory / "rides" / f"{filename}.json", as_json(ride))
                if downloads < args.max_downloads:
                    gpx = client.download_gpx(ride_id)
                    save(directory / "gpx" / f"{filename}.gpx", gpx)
                    reports.append(analyze(ride, gpx, args.timezone))
                    downloads += 1
        summary = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                   "pagination": pagination, "pages": pages, "unique_rides": len(seen), "gpx_downloads": downloads,
                   "pagination_complete": complete, "timestamp_checks": reports}
        save(directory / "report.json", as_json(summary))
        print(f"Export successful: {pages} pages, {len(seen)} rides, {downloads} GPX files. Pagination complete: {complete}.")
        return 0
    except ApiError as exc:
        print(f"Export failed: {exc}")
        return 1
    except (ValueError, OSError, KeyError, OverflowError):
        # Exception bodies can contain sensitive paths, server messages or URLs.
        print("Export failed. Check credentials, connectivity, response schema and timezone. No secrets logged.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
