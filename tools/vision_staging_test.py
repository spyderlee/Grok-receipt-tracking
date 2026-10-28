#!/usr/bin/env python3
"""Vision fallback staging test (audit M8).

Runs a folder of REAL receipt photos through the same pipeline the bot uses
(Tesseract, then the vision fallback where the bot would escalate) and compares the
result with what is actually on each receipt. Use it once before go-live, and again
after changing VISION_MODEL or OCR_CONFIDENCE_THRESHOLD.

    # expected.csv: file,amount,date   e.g.  IMG_0012.jpg,123.40,2026-10-26
    venv/bin/python tools/vision_staging_test.py /path/to/receipts --expected expected.csv
    venv/bin/python tools/vision_staging_test.py /path/to/receipts --expected expected.csv --live
    venv/bin/python tools/vision_staging_test.py /path/to/receipts --live --force-vision

Without --live NO vision API call is made (Tesseract only), so it is safe to run anywhere.
With --live it reads VISION_API_KEY / VISION_MODEL / VISION_API_BASE_URL from the
environment or /etc/bots/maid-receipt/env (via --env-file); the key is never printed.
Each --live image is one paid API call. Exit code: 0 if every image with an expected
amount came out right, 1 otherwise, 2 on usage errors.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maid_receipt_bot.images import ALLOWED_EXT  # noqa: E402
from maid_receipt_bot.ocr import Extraction, ReceiptReader  # noqa: E402
from maid_receipt_bot.vision import VisionClient  # noqa: E402


@dataclass
class Row:
    file: str
    expected_amount: Optional[Decimal]
    expected_date: Optional[date]
    ext: Extraction
    forced_vision: Optional[Extraction] = None

    @property
    def final(self) -> Extraction:
        return self.forced_vision or self.ext

    @property
    def amount_ok(self) -> Optional[bool]:
        if self.expected_amount is None:
            return None
        return self.final.amount == self.expected_amount

    @property
    def date_ok(self) -> Optional[bool]:
        if self.expected_date is None:
            return None
        return self.final.receipt_date == self.expected_date


def load_expected(path: Optional[Path]) -> Dict[str, Tuple[Optional[Decimal], Optional[date]]]:
    out: Dict[str, Tuple[Optional[Decimal], Optional[date]]] = {}
    if not path:
        return out
    with path.open(newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            name = (rec.get("file") or "").strip()
            if not name:
                continue
            amt = d = None
            try:
                amt = Decimal((rec.get("amount") or "").strip()) if (rec.get("amount") or "").strip() else None
            except InvalidOperation:
                raise SystemExit(f"expected.csv: bad amount for {name!r}")
            if (rec.get("date") or "").strip():
                d = date.fromisoformat(rec["date"].strip())
            out[name] = (amt, d)
    return out


def find_images(folder: Path) -> List[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT)


async def run(images: List[Path], expected, reader: ReceiptReader, today: date,
              force_vision: bool = False) -> List[Row]:
    rows: List[Row] = []
    for p in images:
        ext = await reader.extract(p, today)
        exp_amt, exp_date = expected.get(p.name, (None, None))
        row = Row(p.name, exp_amt, exp_date, ext)
        if force_vision and reader.vision is not None and ext.engine != "vision":
            try:
                v = await reader.vision.read(p, today)
                row.forced_vision = Extraction(amount=v.amount, receipt_date=v.receipt_date,
                                               merchant=v.merchant, items=v.items, engine="vision",
                                               vision_model=reader.vision.model, vision_raw=v.raw)
            except Exception as exc:  # report, keep going
                row.forced_vision = Extraction(engine="none", vision_error=str(exc)[:200])
        rows.append(row)
    return rows


def _fmt(v) -> str:
    return "" if v is None else str(v)


def report(rows: List[Row], out=None) -> int:
    w = csv.writer(out if out is not None else sys.stdout)
    w.writerow(["file", "engine", "tess_conf", "escalation", "amount", "expected_amount", "amount_ok",
                "date", "expected_date", "date_ok", "merchant", "vision_error"])
    for r in rows:
        f = r.final
        w.writerow([r.file, f.engine, _fmt(r.ext.confidence and round(r.ext.confidence, 1)),
                    _fmt(r.ext.escalation_reason), _fmt(f.amount), _fmt(r.expected_amount),
                    _fmt(r.amount_ok), _fmt(f.receipt_date), _fmt(r.expected_date), _fmt(r.date_ok),
                    _fmt(f.merchant), _fmt(f.vision_error or r.ext.vision_error)])
    checked = [r for r in rows if r.amount_ok is not None]
    good = sum(1 for r in checked if r.amount_ok)
    escalated = sum(1 for r in rows if r.ext.escalation_reason)
    by_vision = sum(1 for r in rows if r.final.engine == "vision")
    print(f"\n# {len(rows)} images, {escalated} escalated by the bot's rules, {by_vision} read by vision;"
          f" amount correct {good}/{len(checked)}", file=sys.stderr)
    return 0 if good == len(checked) else 1


def _load_env_file(path: Optional[Path]) -> None:
    if path and path.exists():
        from dotenv import load_dotenv
        load_dotenv(path, override=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--expected", type=Path, help="CSV with columns file,amount,date")
    ap.add_argument("--live", action="store_true", help="allow real (paid) vision API calls")
    ap.add_argument("--force-vision", action="store_true", help="also call vision on images Tesseract handled")
    ap.add_argument("--env-file", type=Path, default=None, help="e.g. /etc/bots/maid-receipt/env")
    ap.add_argument("--threshold", type=float, default=None)
    args = ap.parse_args(argv)
    if not args.folder.is_dir():
        print(f"not a folder: {args.folder}", file=sys.stderr)
        return 2
    _load_env_file(args.env_file)
    threshold = args.threshold if args.threshold is not None else float(os.environ.get("OCR_CONFIDENCE_THRESHOLD", "70"))
    vision = None
    if args.live:
        key = os.environ.get("VISION_API_KEY", "").strip()
        if not key:
            print("--live needs VISION_API_KEY in the environment or --env-file", file=sys.stderr)
            return 2
        vision = VisionClient(key, os.environ.get("VISION_MODEL", "gpt-4o-mini"),
                              os.environ.get("VISION_API_BASE_URL", "https://api.openai.com/v1"),
                              float(os.environ.get("VISION_TIMEOUT_SECONDS", "60")))
    elif args.force_vision:
        print("--force-vision requires --live", file=sys.stderr)
        return 2
    reader = ReceiptReader(threshold, os.environ.get("OCR_LANG", "eng"), vision)
    tz = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Asia/Hong_Kong"))
    images = find_images(args.folder)
    if not images:
        print("no images found", file=sys.stderr)
        return 2
    rows = asyncio.run(run(images, load_expected(args.expected), reader, datetime.now(tz).date(),
                           args.force_vision))
    return report(rows)


if __name__ == "__main__":
    raise SystemExit(main())
