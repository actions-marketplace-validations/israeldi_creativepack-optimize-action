#!/usr/bin/env python3
"""Check a CreativePack input and ZIP byte budget entirely on the local machine."""

import argparse
import io
import json
from pathlib import Path
import sys
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import ActionError, MAX_ARCHIVE, is_candidate_image, read_archive, zip_members

PAID_PLANS = "https://rapidapi.com/IsraelDiego/api/creativepack/pricing"


def budget_bytes(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 7:
        raise argparse.ArgumentTypeError("Enter a complete ZIP budget in bytes, from 1024 to 8388608.")
    result = int(value)
    if not 1024 <= result <= MAX_ARCHIVE:
        raise argparse.ArgumentTypeError("Enter a complete ZIP budget in bytes, from 1024 to 8388608.")
    return result


def inspect_archive(data, budget):
    contents = zip_members(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        images = [
            entry for entry in archive.infolist()
            if not entry.is_dir() and is_candidate_image(contents[entry.filename][1])
        ]
    input_size = len(data)
    compressed_image_size = sum(entry.compress_size for entry in images)
    outside_candidate_data = input_size - compressed_image_size
    excess = max(0, input_size - budget)
    if excess == 0:
        status = "already_within_budget"
    elif not images:
        status = "no_png_or_jpeg_candidates"
    elif outside_candidate_data >= budget:
        status = "review_nonimage_bytes_and_zip_overhead"
    else:
        status = "candidate_for_optimization"
    report = {
        "status": status,
        "complete_zip_bytes": input_size,
        "budget_bytes": budget,
        "bytes_to_remove": excess,
        "png_jpeg_candidates": len(images),
        "current_candidate_compressed_bytes": compressed_image_size,
        "current_bytes_outside_candidate_compressed_data": outside_candidate_data,
    }
    if status == "candidate_for_optimization":
        report["service_plans_url"] = PAID_PLANS
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check a creative ZIP and the complete archive byte budget without an account."
    )
    parser.add_argument("input", type=Path, help="Existing ZIP to inspect.")
    parser.add_argument("--budget-bytes", required=True, type=budget_bytes)
    args = parser.parse_args(argv)
    try:
        if not args.input.is_file():
            raise ActionError("The input must be an existing regular ZIP file.")
        archive = read_archive(args.input)
        report = inspect_archive(archive, args.budget_bytes)
    except ActionError as error:
        print(f"CreativePack preflight: {error}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
