#!/usr/bin/env python3
"""One bounded CreativePack request for a GitHub Actions build.

Production always uses the fixed consumer gateway over verified HTTPS.
Connection injection exists only as a Python test seam, never as an Action input.
"""

import base64
import binascii
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import ssl
import stat
import sys
import zlib

HOST = "creativepack.p.rapidapi.com"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024


class ActionError(Exception):
    """Fixed, safe diagnostics; no reflected request or response content."""


def unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ActionError("The service response contains duplicate JSON fields.")
        value[key] = item
    return value


def reject_json_constant(_):
    raise ActionError("The service returned nonstandard JSON.")


def workspace_path(root, value, *, existing=False):
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ActionError("Use nonempty workspace-relative paths without control characters.")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ActionError("Input and output paths must stay within the checked-out workspace.")
    target = root / relative
    try:
        resolved = target.resolve(strict=existing)
        resolved.relative_to(root)
        if target.is_symlink() or not target.parent.resolve(strict=True).is_dir():
            raise ValueError()
    except (OSError, ValueError):
        raise ActionError("Use regular workspace files and existing output directories.") from None
    return target


def read_archive(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARCHIVE:
                raise ActionError("The input must be a regular ZIP of at most 8 MiB.")
            data = source.read(MAX_ARCHIVE + 1)
        if not 0 < len(data) <= MAX_ARCHIVE:
            raise ActionError("The input ZIP exceeds 8 MiB.")
        return data
    except OSError:
        raise ActionError("The input ZIP could not be read.") from None


def zip_members(data):
    # No extraction and no evaluation of HTML/JavaScript from the archive.
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= 256:
                raise ActionError("The ZIP must contain between 1 and 256 entries.")
            result, total = {}, 0
            for entry in entries:
                parts = PurePosixPath(entry.filename)
                if (entry.filename in result or parts.is_absolute() or ".." in parts.parts
                        or "\\" in entry.filename or "\x00" in entry.filename
                        or entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16)
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or entry.file_size > MAX_ARCHIVE):
                    raise ActionError("The ZIP contains unsupported paths, entries or compression.")
                with archive.open(entry) as member:
                    content = member.read(MAX_ARCHIVE + 1)
                total += len(content)
                if len(content) != entry.file_size or len(content) > MAX_ARCHIVE or total > MAX_EXPANDED:
                    raise ActionError("The ZIP exceeds supported expanded-size limits.")
                result[entry.filename] = (entry.is_dir(), content)
            return result
    except ActionError:
        raise
    except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, zlib.error):
        raise ActionError("The ZIP failed integrity verification.") from None


def request_result(original, key, budget, mode, connection_factory=None):
    payload = json.dumps({"archive_base64": base64.b64encode(original).decode("ascii"),
                          "budget_bytes": budget, "mode": mode}, separators=(",", ":")).encode()
    factory = connection_factory or http.client.HTTPSConnection
    conn = factory(HOST, timeout=50, context=ssl.create_default_context())
    try:
        conn.request("POST", "/v1/optimize", body=payload, headers={
            "Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
            "Accept-Encoding": "identity", "X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST})
        response = conn.getresponse()
        if response.status != 200:
            # Avoid printing reflected server messages, URLs, file names or headers.
            raise ActionError(f"CreativePack returned HTTP {response.status}; no automatic retry was made.")
        if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
            raise ActionError("The service returned an unsupported compressed response.")
        if (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
            raise ActionError("The service returned an unsupported response type.")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > MAX_RESPONSE):
            raise ActionError("The service response exceeds the size limit or has invalid framing.")
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ActionError("The service response exceeds 16 MiB.")
        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_json,
                               parse_constant=reject_json_constant)
            pending = [value]
            while pending:
                item = pending.pop()
                if isinstance(item, str) and key in item:
                    raise ActionError("The service response unexpectedly contains a credential value.")
                if isinstance(item, list):
                    pending.extend(item)
                if isinstance(item, dict):
                    pending.extend(item.keys())
                    pending.extend(item.values())
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise ActionError("The service returned invalid JSON.") from None
    except ActionError:
        raise
    except (OSError, http.client.HTTPException):
        raise ActionError("The verified HTTPS request could not complete; no automatic retry was made.") from None
    finally:
        conn.close()


def verify_result(value, original, before, budget, mode):
    if not isinstance(value, dict) or set(value) != {"archive_base64", "report"}:
        raise ActionError("The service returned an unsupported result contract.")
    report, encoded = value["report"], value["archive_base64"]
    try:
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ARCHIVE + 2) // 3):
            raise ValueError()
        output = base64.b64decode(encoded, validate=True)
        if base64.b64encode(output).decode("ascii") != encoded or not 0 < len(output) <= min(len(original), MAX_ARCHIVE):
            raise ValueError()
    except (ValueError, TypeError, binascii.Error):
        raise ActionError("The returned ZIP failed encoding or size verification.") from None
    expected = {"schema_version": "1.0", "requested_mode": mode, "budget_bytes": budget,
                "status": "within_budget" if len(output) <= budget else "over_budget",
                "input_bytes": len(original), "output_bytes": len(output),
                "saved_bytes": len(original) - len(output),
                "input_sha256": hashlib.sha256(original).hexdigest(),
                "output_sha256": hashlib.sha256(output).hexdigest()}
    if not isinstance(report, dict) or any(type(report.get(k)) is not type(v) or report[k] != v for k, v in expected.items()):
        raise ActionError("The result report does not match the submitted archive and returned bytes.")
    after = zip_members(output)
    if list(before) != list(after):
        raise ActionError("The result changed the ZIP member paths or order.")
    entries = report.get("files")
    if not isinstance(entries, list) or len(entries) != len(before):
        raise ActionError("The result file manifest is incomplete.")
    for (name, (directory, old)), entry in zip(before.items(), entries):
        new_directory, new = after[name]
        expected_file = {"path": name, "is_directory": directory, "input_bytes": len(old),
                         "output_bytes": len(new), "input_sha256": hashlib.sha256(old).hexdigest(),
                         "output_sha256": hashlib.sha256(new).hexdigest()}
        if not isinstance(entry, dict) or any(type(entry.get(k)) is not type(v) or entry[k] != v for k, v in expected_file.items()):
            raise ActionError("The result file manifest does not match the verified ZIP.")
        image = old.startswith(b"\x89PNG\r\n\x1a\n") or old.startswith(b"\xff\xd8\xff")
        if new_directory != directory or ((directory or not image) and old != new):
            raise ActionError("A nonimage ZIP member changed.")
    return output, report


def save_outputs(archive, report_path, output, report):
    # Allocate both exclusive files first, so existing files remain intact.
    created = []
    try:
        for path in (archive, report_path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            created.append((path, fd, os.fstat(fd)))
        values = [output, (json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()]
        for (_, fd, _), data in zip(created, values):
            with os.fdopen(os.dup(fd), "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
    except (OSError, ValueError, TypeError):
        for path, _, info in created:
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                    path.unlink()
            except OSError:
                pass
        raise ActionError("The new result files could not both be saved; existing files were preserved.") from None
    finally:
        for _, fd, _ in created:
            os.close(fd)


def execute(env, connection_factory=None):
    try:
        root = Path(env.get("GITHUB_WORKSPACE", "")).resolve(strict=True)
        if not env.get("GITHUB_WORKSPACE") or not root.is_dir():
            raise ActionError("GITHUB_WORKSPACE must identify an existing checked-out directory.")
        key = env.get("CREATIVEPACK_RAPIDAPI_KEY", "")
        if not 16 <= len(key) <= 512 or any(not 33 <= ord(c) <= 126 for c in key):
            raise ActionError("Supply your subscribed RapidAPI key through a GitHub Actions secret.")
        budget_text = env.get("CREATIVEPACK_BUDGET_BYTES", "")
        if not budget_text.isascii() or not budget_text.isdecimal() or not 1024 <= int(budget_text) <= MAX_ARCHIVE:
            raise ActionError("budget-bytes must be an integer from 1024 to 8388608.")
        budget = int(budget_text)
        mode = env.get("CREATIVEPACK_MODE", "lossless")
        fail = env.get("CREATIVEPACK_FAIL_ON_BUDGET", "true")
        if mode not in ("lossless", "balanced") or fail not in ("true", "false"):
            raise ActionError("Use lossless/balanced mode and true/false for fail-on-budget.")
        source = workspace_path(root, env.get("CREATIVEPACK_INPUT", ""), existing=True)
        archive = workspace_path(root, env.get("CREATIVEPACK_OUTPUT", "creativepack-optimized.zip"))
        report_path = workspace_path(root, env.get("CREATIVEPACK_REPORT", "creativepack-report.json"))
        if len({source.resolve(), archive.resolve(), report_path.resolve()}) != 3:
            raise ActionError("Input ZIP, output ZIP and report must be three different files.")
        if any(p.exists() or p.is_symlink() for p in (archive, report_path)):
            raise ActionError("Output and report paths must be new; existing files were preserved.")
        original = read_archive(source)
        before = zip_members(original)
        value = request_result(original, key, budget, mode, connection_factory)
        output, report = verify_result(value, original, before, budget, mode)
        save_outputs(archive, report_path, output, report)
        outputs = {"archive": archive.relative_to(root).as_posix(), "report": report_path.relative_to(root).as_posix(),
                   "status": report["status"], "input-bytes": len(original), "output-bytes": len(output),
                   "saved-bytes": report["saved_bytes"], "output-sha256": report["output_sha256"]}
        if env.get("GITHUB_OUTPUT"):
            try:
                with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as target:
                    target.write("".join(f"{k}={v}\n" for k, v in outputs.items()))
            except OSError:
                raise ActionError("Result files were saved, but GitHub step outputs could not be written.") from None
        print(f"CreativePack: {report['status']}; ZIP {len(original)} → {len(output)} bytes; saved {report['saved_bytes']} bytes.")
        if report["status"] == "over_budget" and fail == "true":
            print("::error::The verified ZIP remains over the requested byte budget; result files were saved.", file=sys.stderr)
            return 2
        return 0
    except ActionError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, OverflowError):
        print("::error::The Action could not read its configuration or local files.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(execute(os.environ))
