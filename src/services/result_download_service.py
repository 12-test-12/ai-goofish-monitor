"""Build large exports on disk in bounded batches, without blocking the API loop."""
import asyncio
from pathlib import Path
import tempfile

from src.services.result_export_service import write_results_csv
from src.services.result_file_service import enrich_records_with_price_insight
from src.services.result_storage_service import iter_result_batches, write_result_ndjson


def _create_download(filename: str, csv_export: bool, options: dict) -> str:
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", suffix=".csv" if csv_export else ".jsonl", delete=False) as output:
            path = output.name
            if csv_export:
                write_results_csv([], output)
                for batch in iter_result_batches(filename, **options):
                    write_results_csv(enrich_records_with_price_insight(batch, filename), output, include_header=False)
            else:
                write_result_ndjson(filename, output)
        return path
    except BaseException:
        if path:
            Path(path).unlink(missing_ok=True)
        raise


async def create_result_download(filename: str, *, csv_export=False, **options) -> str:
    worker = asyncio.create_task(asyncio.to_thread(_create_download, filename, csv_export, options))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            path = await worker
        except Exception:
            pass
        else:
            Path(path).unlink(missing_ok=True)
        raise
