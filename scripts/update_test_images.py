#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Keep assets/test.images.yaml in sync with the images defined in the charm repositories.

Each entry under `charms` points to a file in a charm repository (at a given branch) and to a
dotted key path inside that file, whose value is the image reference. This script fetches those
files, resolves the paths and updates the `image` fields accordingly. Images listed under
`static` are not sourced from charms and are never modified.
"""

import argparse
import difflib
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
import yaml

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "assets" / "test.images.yaml"
RAW_BASE_URL = "https://raw.githubusercontent.com"
DEFAULT_REGISTRY = "docker.io"
REQUEST_TIMEOUT = 30

EXIT_OK = 0
EXIT_OUTDATED = 1
EXIT_ERROR = 2

logger = logging.getLogger("update_test_images")

Fetcher = Callable[[str], str]


class ImagesConfigError(Exception):
    """Raised when the images file or an upstream source cannot be processed."""


@dataclass(frozen=True)
class Change:
    """An image whose upstream value differs from the one in the images file."""

    repo: str
    file: str
    path: str
    old: str
    new: str

    def __str__(self):
        return f"{self.repo}:{self.file}#{self.path}: {self.old} -> {self.new}"


def load_config(path: Path) -> dict:
    """Load and validate the images file."""
    try:
        cfg = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ImagesConfigError(f"Cannot read {path}: {e}") from e
    validate_config(cfg)
    return cfg


def _require_str(value, where: str):
    if not isinstance(value, str) or not value.strip():
        raise ImagesConfigError(f"{where} must be a non-empty string")


def _validate_repo(name: str, repo) -> None:
    where = f"charms.{name}"
    if not isinstance(repo, dict):
        raise ImagesConfigError(f"{where} must be a mapping")
    for key in ("repo", "branch"):
        _require_str(repo.get(key), f"{where}.{key}")
    parse_github_repo(repo["repo"])
    images = repo.get("images")
    if not isinstance(images, list) or not images:
        raise ImagesConfigError(f"{where}.images must be a non-empty list")
    for i, entry in enumerate(images):
        if not isinstance(entry, dict):
            raise ImagesConfigError(f"{where}.images[{i}] must be a mapping")
        for key in ("file", "path", "image"):
            _require_str(entry.get(key), f"{where}.images[{i}].{key}")


def validate_config(cfg) -> None:
    """Validate the structure of the images file, raising ImagesConfigError on failure."""
    if not isinstance(cfg, dict):
        raise ImagesConfigError("Images file must be a mapping")
    unknown = set(cfg) - {"charms", "static"}
    if unknown:
        raise ImagesConfigError(f"Unknown top-level keys: {sorted(unknown)}")

    charms = cfg.get("charms", {})
    if not isinstance(charms, dict):
        raise ImagesConfigError("'charms' must be a mapping of repository name to definition")
    for name, repo in charms.items():
        _validate_repo(name, repo)

    static = cfg.get("static", [])
    if not isinstance(static, list):
        raise ImagesConfigError("'static' must be a list of images")
    for i, image in enumerate(static):
        _require_str(image, f"static[{i}]")


def parse_github_repo(url: str) -> str:
    """Return `owner/name` from a GitHub repository URL."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc != "github.com" or len(parts) != 2:
        raise ImagesConfigError(f"Not a GitHub repository URL: {url}")
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def raw_url(repo_url: str, branch: str, file: str) -> str:
    """Build the raw.githubusercontent.com URL of a file in a repository at a branch."""
    return f"{RAW_BASE_URL}/{parse_github_repo(repo_url)}/{branch}/{file.lstrip('/')}"


def http_fetch(url: str) -> str:
    """Fetch a URL as text, authenticating with GITHUB_TOKEN if available."""
    headers = {}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as e:
        raise ImagesConfigError(f"Failed to fetch {url}: {e}") from e
    return response.text


def parse_content(content: str, file: str):
    """Parse a source file as JSON or YAML based on its extension."""
    suffix = Path(file).suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(content)
        if suffix in (".yaml", ".yml"):
            return yaml.safe_load(content)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise ImagesConfigError(f"Cannot parse {file}: {e}") from e
    raise ImagesConfigError(f"Unsupported file type for {file} (expected .json, .yaml or .yml)")


def resolve_path(data, path: str):
    """Return the value at a dotted key path (e.g. `options.executor-image.default`)."""
    current = data
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            raise KeyError(path)
        current = current[key]
    return current


def normalize_image(ref: str) -> str:
    """Return a fully qualified image reference, adding the default registry if missing."""
    ref = ref.strip().strip("\"'")
    first, sep, _ = ref.partition("/")
    has_registry = sep and ("." in first or ":" in first or first == "localhost")
    return ref if has_registry else f"{DEFAULT_REGISTRY}/{ref}"


def compute_updates(cfg: dict, fetcher: Fetcher = http_fetch) -> list[Change]:
    """Compare each charm-sourced image with its upstream value and return the differences."""
    cache: dict[str, object] = {}
    changes = []
    for name, repo in cfg.get("charms", {}).items():
        for entry in repo["images"]:
            url = raw_url(repo["repo"], repo["branch"], entry["file"])
            if url not in cache:
                logger.debug("Fetching %s", url)
                cache[url] = parse_content(fetcher(url), entry["file"])
            try:
                value = resolve_path(cache[url], entry["path"])
            except KeyError:
                raise ImagesConfigError(
                    f"Path '{entry['path']}' not found in {entry['file']} of {name} "
                    f"(branch {repo['branch']})"
                )
            if not isinstance(value, str) or not value.strip():
                raise ImagesConfigError(
                    f"Value at '{entry['path']}' in {entry['file']} of {name} is not an image "
                    f"reference: {value!r}"
                )
            new = normalize_image(value)
            if new != entry["image"]:
                changes.append(Change(name, entry["file"], entry["path"], entry["image"], new))
    return changes


def apply_updates(cfg: dict, changes: list[Change]) -> None:
    """Update the `image` fields of the config in place."""
    by_key = {(c.repo, c.file, c.path): c.new for c in changes}
    for name, repo in cfg.get("charms", {}).items():
        for entry in repo["images"]:
            key = (name, entry["file"], entry["path"])
            if key in by_key:
                entry["image"] = by_key[key]


def dump_config(cfg: dict) -> str:
    """Serialize the images file preserving key order."""
    return yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)


def write_config(path: Path, cfg: dict) -> None:
    """Write the images file preserving key order."""
    Path(path).write_text(dump_config(cfg))


def config_diff(path: Path, cfg: dict) -> str:
    """Return a unified diff between the images file on disk and the given config."""
    before = Path(path).read_text().splitlines(keepends=True)
    after = dump_config(cfg).splitlines(keepends=True)
    return "".join(difflib.unified_diff(before, after, fromfile=str(path), tofile=str(path)))


def list_images(cfg: dict) -> list[str]:
    """Return all images (charm-sourced and static), sorted and de-duplicated."""
    images = {e["image"] for r in cfg.get("charms", {}).values() for e in r["images"]}
    images.update(cfg.get("static", []))
    return sorted(images)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--file", type=Path, default=DEFAULT_FILE, help="images file (default: %(default)s)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit with 1 if any image is outdated",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="do not write; print the diff that would be applied to the images file",
    )
    mode.add_argument(
        "--list",
        action="store_true",
        help="print all images, one per line, without fetching upstream",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None, fetcher: Fetcher = http_fetch) -> int:
    """Entry point."""
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        cfg = load_config(args.file)
        if args.list:
            print("\n".join(list_images(cfg)))
            return EXIT_OK
        changes = compute_updates(cfg, fetcher)
    except ImagesConfigError as e:
        logger.error("%s", e)
        return EXIT_ERROR

    if not changes:
        logger.info("All images are up to date")
        return EXIT_OK
    for change in changes:
        logger.info("%s", change)
    if args.check:
        logger.error("%d image(s) outdated", len(changes))
        return EXIT_OUTDATED
    apply_updates(cfg, changes)
    if args.dry_run:
        print(config_diff(args.file, cfg), end="")
        logger.info("Dry run: %d image(s) would be updated in %s", len(changes), args.file)
        return EXIT_OK
    write_config(args.file, cfg)
    logger.info("Updated %d image(s) in %s", len(changes), args.file)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
