#!/usr/bin/env python3
"""
Collect files from GitHub URLs or repo-relative paths listed in text files.

The downloaded files are copied as raw bytes, so the script is language
agnostic. It works the same for Python, JavaScript, TypeScript, CSS, Java,
YAML, JSON, images, and any other file type GitHub can serve.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


COMMENT_PREFIXES = ("#", "//")


class ReferenceError(ValueError):
    pass


def iter_references(input_dir: Path) -> list[str]:
    refs: list[str] = []

    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"Skipping non-text file: {path}", file=sys.stderr)
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            ref = line.strip()

            if not ref or ref.startswith(COMMENT_PREFIXES):
                continue

            refs.append(ref)
            print(f"Queued {ref} from {path}:{line_number}")

    return refs


def clean_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(urllib.parse.unquote(value).lstrip("/"))

    if path.is_absolute() or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise ReferenceError(f"Unsafe output path: {value}")

    return path


def safe_output_path(
    output_dir: Path,
    relative_path: PurePosixPath,
) -> Path:
    destination = (
        output_dir / Path(*relative_path.parts)
    ).resolve()

    output_root = output_dir.resolve()

    if (
        output_root != destination
        and output_root not in destination.parents
    ):
        raise ReferenceError(
            f"Resolved path escapes output directory: {relative_path}"
        )

    return destination


def github_blob_to_raw(
    parsed: urllib.parse.ParseResult,
) -> tuple[str, PurePosixPath]:

    parts = [
        part
        for part in parsed.path.split("/")
        if part
    ]

    if len(parts) < 5 or parts[2] != "blob":
        raise ReferenceError(
            f"Unsupported GitHub URL: {parsed.geturl()}"
        )

    owner, repo, _, ref = parts[:4]

    file_path = "/".join(parts[4:])

    relative_path = clean_relative_path(file_path)

    raw_url = (
        f"https://raw.githubusercontent.com/"
        f"{owner}/{repo}/{ref}/{file_path}"
    )

    return raw_url, relative_path


def raw_github_to_reference(
    parsed: urllib.parse.ParseResult,
) -> tuple[str, PurePosixPath]:

    parts = [
        part
        for part in parsed.path.split("/")
        if part
    ]

    if len(parts) < 4:
        raise ReferenceError(
            f"Unsupported raw GitHub URL: {parsed.geturl()}"
        )

    file_path = "/".join(parts[3:])

    return parsed.geturl(), clean_relative_path(file_path)


def reference_to_download(
    ref: str,
    repo: str | None,
    git_ref: str | None,
    input_dir: Path,
) -> tuple[str, PurePosixPath, bool]:

    parsed = urllib.parse.urlparse(ref)

    if parsed.scheme in ("http", "https"):
        host = parsed.netloc.lower()

        if host == "github.com":
            url, relative_path = github_blob_to_raw(parsed)
            return url, relative_path, False

        if host == "raw.githubusercontent.com":
            url, relative_path = raw_github_to_reference(parsed)
            return url, relative_path, False

        raise ReferenceError(
            f"Only GitHub file URLs are supported: {ref}"
        )

    # Try local file paths first
    candidate = Path(ref)

    if candidate.is_absolute():
        if candidate.exists() and candidate.is_file():
            relative_path = PurePosixPath(candidate.name)
            return str(candidate.resolve()), relative_path, True
    else:
        cand_input = (input_dir / ref)
        cand_cwd = (Path.cwd() / ref)

        if cand_input.exists() and cand_input.is_file():
            relative_path = clean_relative_path(ref)
            return str(cand_input.resolve()), relative_path, True

        if cand_cwd.exists() and cand_cwd.is_file():
            relative_path = clean_relative_path(ref)
            return str(cand_cwd.resolve()), relative_path, True

    # Fall back to repo-relative behavior
    if not repo or not git_ref:
        raise ReferenceError(
            f"Plain path requires --repo and --ref or a local file: {ref}"
        )

    relative_path = clean_relative_path(ref)

    quoted_path = "/".join(
        urllib.parse.quote(part)
        for part in relative_path.parts
    )

    raw_url = (
        f"https://raw.githubusercontent.com/"
        f"{repo}/{git_ref}/{quoted_path}"
    )

    return raw_url, relative_path, False


def download_file(
    url: str,
    token: str | None,
) -> bytes:

    headers = {
        "User-Agent": "collect-github-files/1.0"
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        url,
        headers=headers,
    )

    with urllib.request.urlopen(
        request,
        timeout=30,
    ) as response:
        return response.read()


def collect_files(
    input_dir: Path,
    output_dir: Path,
    repo: str | None,
    git_ref: str | None,
    token: str | None,
    dry_run: bool,
) -> int:

    if not input_dir.is_dir():
        raise ReferenceError(
            f"Input directory does not exist: {input_dir}"
        )

    refs = iter_references(input_dir)

    if not refs:
        print("No file references found.")
        return 0

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    failures = 0

    for ref in refs:
        try:
            source, relative_path, is_local = reference_to_download(
                ref,
                repo,
                git_ref,
                input_dir,
            )

            destination = safe_output_path(
                output_dir,
                relative_path,
            )

            if dry_run:
                if is_local:
                    print(f"DRY RUN local {source} -> {destination}")
                else:
                    print(f"DRY RUN {source} -> {destination}")
                continue

            if is_local:
                content = Path(source).read_bytes()
            else:
                content = download_file(
                    source,
                    token,
                )

            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            destination.write_bytes(content)

            print(f"Wrote {destination}")

        except (
            ReferenceError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:

            failures += 1

            print(
                f"Failed {ref}: {exc}",
                file=sys.stderr,
            )

    return failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download GitHub file contents listed in files "
            "under an input directory. Downloaded file "
            "contents are copied as raw bytes."
        )
    )

    parser.add_argument(
        "input_dir",
        type=Path,
        help="Directory containing text files."
    )

    parser.add_argument(
        "output_dir",
        type=Path,
        help="Directory where files will be written."
    )

    parser.add_argument(
        "--repo",
        help="GitHub repo as OWNER/REPO"
    )

    parser.add_argument(
        "--ref",
        dest="git_ref",
        help="Branch, tag, or commit SHA"
    )

    parser.add_argument(
        "--token",
        help="GitHub token"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without downloading"
    )

    return parser


def main() -> int:
    parser = build_parser()

    args = parser.parse_args()

    try:
        failures = collect_files(
            args.input_dir,
            args.output_dir,
            args.repo,
            args.git_ref,
            args.token,
            args.dry_run,
        )

    except ReferenceError as exc:
        parser.error(str(exc))

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
