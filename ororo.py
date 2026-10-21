#!/usr/bin/env python3
"""Download movies and shows made available by an ORORO subscription."""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import getpass
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


API_DOMAINS = ("front.ororo-mirror.tv", "front.ororo.tv")
VERSION = "0.2.0"
USER_AGENT = f"ororo/{VERSION}"
CHUNK_SIZE = 1024 * 1024
QUALITY_LEVELS = (240, 360, 480, 720, 1080, 1440, 2160)
T = TypeVar("T")
LOCAL_ERRNOS = {errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EPERM, errno.EROFS, errno.EIO, errno.EFBIG, errno.ENOTDIR}


class DownloadError(Exception):
    """An error that can be shown directly to the user."""


class AuthenticationError(DownloadError):
    """The stored or supplied ORORO credentials were rejected."""


class FatalError(DownloadError):
    """An error that must stop the whole batch."""


class TransferError(DownloadError):
    """A transfer that can be retried."""


@dataclass(frozen=True)
class Page:
    kind: str
    slug: str
    url: str


@dataclass(frozen=True)
class Target:
    kind: str
    media_id: int
    directory: Path
    stem: str
    label: str
    resolution: str

    @property
    def video_path(self) -> Path:
        return self.directory / f"{self.stem}.mp4"


@dataclass(frozen=True)
class MediaSource:
    mode: str
    url: str
    quality: str
    size: int | None
    exact_size: bool
    program_id: int | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class HLSVariant:
    program_id: int
    quality: str
    bandwidth: int
    url: str


@dataclass(frozen=True)
class PlanItem:
    target: Target
    source: MediaSource
    action: str
    current_quality: str | None
    destination: Path
    resume_bytes: int
    partial_bytes: int
    download_bytes: int | None
    additional_space_bytes: int | None
    existing_bytes: int
    subtitles: tuple[tuple[str, str], ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Credentials:
    user: str
    password: str = field(repr=False)
    path: Path
    prompted: bool = False


def parse_page_url(value: str) -> Page:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise DownloadError(f"expected an HTTP(S) ORORO page URL: {value}")
    if host not in {"ororo.tv", "www.ororo.tv"}:
        raise DownloadError(f"unsupported host in URL: {value}")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    for kind in ("shows", "movies"):
        if kind in parts:
            pos = parts.index(kind)
            if pos + 1 < len(parts):
                return Page(kind=kind, slug=parts[pos + 1].strip().lower(), url=value)
    raise DownloadError(f"expected an ORORO show or movie URL: {value}")


def pages_from_inputs(urls: list[str], input_file: Path | None) -> list[Page]:
    pages = [parse_page_url(value) for value in urls]
    if input_file is not None:
        input_file = input_file.expanduser()
        try:
            lines = input_file.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            raise DownloadError(f"could not read input file {input_file}: {exc}") from exc
        for line_number, line in enumerate(lines, start=1):
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            try:
                if any(character.isspace() for character in value):
                    raise DownloadError("expected one ORORO URL per line")
                pages.append(parse_page_url(value))
            except DownloadError as exc:
                raise DownloadError(f"{input_file}:{line_number}: {exc}") from exc
    if not pages:
        raise DownloadError("no URLs supplied; pass ORORO page URLs or use --input-file FILE")
    unique: dict[tuple[str, str], Page] = {}
    for page in pages:
        unique.setdefault((page.kind, page.slug), page)
    return list(unique.values())


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def safe_name(value: str, fallback: str = "Untitled") -> str:
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def number(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise DownloadError(f"invalid episode number from ORORO: {value!r}") from exc


def parse_number_set(values: list[str] | None) -> set[int] | None:
    if not values:
        return None
    result: set[int] = set()
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if not re.fullmatch(r"\d+(-\d+)?", part):
                raise DownloadError(f"invalid number or range: {part}; expected e.g. 1,3-4")
            first_text, _, last_text = part.partition("-")
            first, last = int(first_text), int(last_text or first_text)
            if first > last:
                raise DownloadError(f"invalid number range: {part}")
            result.update(range(first, last + 1))
    return result


def human_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    amount = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    raise AssertionError("unreachable")


def quality_height(value: str) -> int:
    match = re.fullmatch(r"(\d+)p", value.casefold())
    if not match:
        raise DownloadError(f"invalid quality: {value}")
    height = int(match.group(1))
    if height not in QUALITY_LEVELS:
        allowed = ", ".join(f"{item}p" for item in QUALITY_LEVELS)
        raise DownloadError(f"unsupported quality {value!r}; choose best or one of {allowed}")
    return height


def parse_max_quality(value: str) -> int | None:
    return None if value.casefold() == "best" else quality_height(value)


def config_directory() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(value) if value and Path(value).is_absolute() else Path.home() / ".config"
    return root / "ororo"


def default_credentials_path() -> Path:
    return config_directory() / "credentials.json"


def load_config(path: Path | None) -> dict:
    explicit = path is not None
    path = path.expanduser() if explicit else config_directory() / "config.json"
    if not path.exists() and not explicit:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DownloadError(f"could not read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DownloadError(f"config must be a JSON object: {path}")
    unknown = set(data) - {"output", "max_quality", "subs", "delay"}
    if unknown:
        raise DownloadError(f"unknown config options in {path}: {', '.join(sorted(unknown))}")
    for key in ("output", "max_quality", "subs"):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise DownloadError(f"config option {key} must be a non-empty string")
    if "delay" in data and (isinstance(data["delay"], bool) or not isinstance(data["delay"], (int, float))):
        raise DownloadError("config option delay must be a number")
    return data


def load_credentials(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise DownloadError(
            f"credentials file is readable by other users: {path}; run chmod 600 {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DownloadError(f"could not read credentials file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DownloadError(f"invalid credentials file: {path}; expected a JSON object")
    user = data.get("user")
    password = data.get("password")
    if not isinstance(user, str) or not isinstance(password, str) or not user or not password:
        raise DownloadError(f"invalid credentials file: {path}")
    return user, password


def save_credentials(path: Path, user: str, password: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".credentials.", dir=path.parent, text=True)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump({"version": 1, "user": user, "password": password}, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def interactive(args: argparse.Namespace) -> bool:
    return not args.non_interactive and not args.json and sys.stdin.isatty()


def resolve_credentials(args: argparse.Namespace, *, login: bool = False) -> Credentials:
    path = args.credentials_file.expanduser() if args.credentials_file else default_credentials_path()
    user = args.user or os.environ.get("ORORO_USER")
    password = os.environ.get("ORORO_PASSWORD")
    if user and password:
        return Credentials(user, password, path)
    if login and user:
        saved_user, saved_password = None, None
    else:
        try:
            saved_user, saved_password = load_credentials(path)
        except DownloadError:
            if not login or not interactive(args):
                raise
            print("saved credentials could not be read; enter credentials again", file=sys.stderr)
            saved_user, saved_password = None, None
    user = user or saved_user
    password = password or (saved_password if not login and user == saved_user else None)
    prompted = False
    if not user or not password:
        if not interactive(args):
            raise AuthenticationError("ORORO credentials are missing; run ororo login in a terminal or set ORORO_USER and ORORO_PASSWORD")
        if not user:
            print("ORORO login: ", end="", file=sys.stderr, flush=True)
            user = input().strip()
            prompted = True
        if not password:
            password = getpass.getpass("ORORO password: ", stream=sys.stderr)
            prompted = True
    if not user or not password:
        raise AuthenticationError("ORORO login and password are required")
    return Credentials(user, password, path, prompted)


class OroroAPI:
    def __init__(self, user: str, password: str, timeout: float = 30) -> None:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.authorization = f"Basic {token}"
        self.timeout = timeout
        self.active_domain: str | None = None
        self.catalogs: dict[str, list[dict]] = {}

    def get_json(self, path: str) -> dict:
        domains = list(API_DOMAINS)
        if self.active_domain in domains:
            domains.remove(self.active_domain)
            domains.insert(0, self.active_domain)
        last_error: Exception | None = None
        for domain in domains:
            request = Request(
                f"https://{domain}/api/v2{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": self.authorization,
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                def fetch():
                    with urlopen(request, timeout=self.timeout) as response:
                        return response.read()
                body = retry(fetch, "ORORO API")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise DownloadError("ORORO returned an invalid JSON object")
                self.active_domain = domain
                return data
            except HTTPError as exc:
                if exc.code == 401:
                    raise AuthenticationError("ORORO rejected the login or password") from exc
                if exc.code == 402:
                    raise FatalError("ORORO says the subscription does not allow this request") from exc
                if transient_error(exc):
                    last_error = exc
                    continue
                raise DownloadError(f"ORORO API returned HTTP {exc.code} for {path}") from exc
            except (URLError, TimeoutError, OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
                last_error = exc
        raise DownloadError(f"could not reach the ORORO API: {last_error}")

    def catalog(self, kind: str) -> list[dict]:
        catalog = self.catalogs.get(kind)
        if catalog is None:
            catalog = self.get_json(f"/{kind}").get(kind)
        if not isinstance(catalog, list) or any(not isinstance(item, dict) for item in catalog):
            raise DownloadError(f"ORORO returned an invalid {kind} catalog")
        self.catalogs[kind] = catalog
        return catalog

    def find(self, page: Page) -> dict:
        singular = "show" if page.kind == "shows" else "movie"
        catalog = self.catalog(page.kind)

        exact: list[dict] = []
        for item in catalog:
            candidates = {
                str(item.get("slug", "")).casefold(),
                slugify(str(item.get("name", ""))),
            }
            if page.slug in candidates:
                exact.append(item)
        if not exact:
            # ORORO also uses literal spellings: Léon -> l-on, A & B -> a-b.
            exact = [item for item in catalog if page.slug == re.sub(
                r"[^a-z0-9]+", "-", str(item.get("name", "")).lower()
            ).strip("-")]
        if len(exact) == 1:
            return exact[0]
        if not exact:
            raise DownloadError(f"could not find {singular} for URL slug {page.slug!r}")
        names = ", ".join(str(item.get("name")) for item in exact)
        raise DownloadError(f"URL slug {page.slug!r} is ambiguous: {names}")

    def detail(self, kind: str, media_id: int) -> dict:
        endpoint = "episodes" if kind == "episode" else "movies"
        return self.get_json(f"/{endpoint}/{media_id}")


def object_list(data: dict, key: str) -> list[dict]:
    items = data.get(key) or []
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise DownloadError(f"ORORO returned an invalid {key} list")
    return items


def search_catalog(api: OroroAPI, query: str, kind: str) -> list[dict]:
    words = unicodedata.normalize("NFKC", query).casefold().split()
    results = []
    for category in (("movies", "shows") if kind == "all" else (kind,)):
        for entry in api.catalog(category):
            name = str(entry.get("name") or "")
            haystack = unicodedata.normalize("NFKC", f"{name} {entry.get('year') or ''}").casefold()
            if all(word in haystack for word in words):
                slug = str(entry.get("slug") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))
                results.append({"kind": category, "id": entry.get("id"), "name": name,
                                "year": entry.get("year"), "url": f"https://ororo.tv/en/{category}/{slug}"})
    return sorted(results, key=lambda item: (item["name"].casefold(), str(item["year"] or ""), item["kind"]))


def page_info(api: OroroAPI, page: Page, seasons: set[int] | None, episodes: set[int] | None) -> dict:
    entry = api.find(page)
    detail = api.get_json(f"/shows/{number(entry.get('id'))}") if page.kind == "shows" else api.detail("movie", number(entry.get("id")))
    data = {**entry, **detail}
    result = {"kind": page.kind, "id": entry.get("id"), "name": data.get("name"),
              "year": data.get("year"), "description": data.get("description") or data.get("desc") or "",
              "resolution": data.get("resolution"), "url": page.url}
    if page.kind == "shows":
        selected = []
        for episode in object_list(data, "episodes"):
            season, index = number(episode.get("season")), number(episode.get("number"))
            if (seasons is None or season in seasons) and (episodes is None or index in episodes):
                selected.append({"season": season, "episode": index, "name": episode.get("name"), "id": episode.get("id")})
        result["episodes"] = sorted(selected, key=lambda item: (item["season"], item["episode"]))
        result["seasons"] = sorted({item["season"] for item in selected})
    else:
        result["subtitles"] = sorted({str(item.get("lang")) for item in object_list(data, "subtitles") if item.get("lang")})
    return result


def targets_for_page(
    api: OroroAPI,
    page: Page,
    output: Path,
    seasons: set[int] | None,
    episodes: set[int] | None,
) -> list[Target]:
    entry = api.find(page)
    if page.kind == "movies":
        if not entry.get("name"):
            raise DownloadError("ORORO returned a movie without a name")
        title = safe_name(str(entry["name"]))
        year = safe_name(str(entry.get("year") or ""), "")
        stem = f"{title} ({year})" if year else title
        return [
            Target(
                kind="movie",
                media_id=number(entry.get("id")),
                directory=output / stem,
                stem=stem,
                label=stem,
                resolution=str(entry.get("resolution") or "unknown"),
            )
        ]

    show = api.get_json(f"/shows/{number(entry.get('id'))}")
    if not show.get("name"):
        raise DownloadError("ORORO returned a show without a name")
    title = safe_name(str(show["name"]))
    year = safe_name(str(show.get("year") or ""), "")
    root_name = f"{title} ({year})" if year else title
    result: list[Target] = []
    for episode in object_list(show, "episodes"):
        season_no = number(episode.get("season"))
        episode_no = number(episode.get("number"))
        if seasons is not None and season_no not in seasons:
            continue
        if episodes is not None and episode_no not in episodes:
            continue
        episode_title = safe_name(str(episode.get("name") or "Episode"), "Episode")
        stem = f"{title} - S{season_no:02d}E{episode_no:02d} - {episode_title}"
        result.append(
            Target(
                kind="episode",
                media_id=number(episode.get("id")),
                directory=output / root_name / f"Season {season_no:02d}",
                stem=stem,
                label=f"{root_name} S{season_no:02d}E{episode_no:02d}",
                resolution=str(episode.get("resolution") or "unknown"),
            )
        )
    result.sort(key=lambda item: item.stem)
    if not result:
        raise DownloadError(f"no episodes matched the filters for {root_name}")
    return result


def request_url(
    url: str,
    *,
    method: str = "GET",
    start: int | None = None,
    end: int | None = None,
    validator: str | None = None,
):
    headers = {"User-Agent": USER_AGENT}
    if start is not None:
        suffix = "" if end is None else str(end)
        headers["Range"] = f"bytes={start}-{suffix}"
        if validator:
            headers["If-Range"] = validator
    return urlopen(Request(url, headers=headers, method=method), timeout=60)


def probe_source(url: str) -> tuple[int | None, str | None, str | None]:
    try:
        with request_url(url, method="HEAD") as response:
            value = response.headers.get("Content-Length")
            return (int(value) if value else None, response.headers.get("ETag"), response.headers.get("Last-Modified"))
    except HTTPError as exc:
        if exc.code not in {403, 405}:
            raise
    with request_url(url, start=0, end=0) as response:
        content_range = response.headers.get("Content-Range", "")
        match = re.search(r"/(\d+)$", content_range)
        size = int(match.group(1)) if match else None
        if size is None and response.getcode() == 200:
            value = response.headers.get("Content-Length")
            size = int(value) if value else None
        return size, response.headers.get("ETag"), response.headers.get("Last-Modified")


def chosen_quality(detail: dict, fallback: str) -> str:
    download_url = str(detail.get("download_url") or "")
    download_path = urlparse(download_url).path
    versions = detail.get("versions") or {}
    if not isinstance(versions, dict):
        raise DownloadError("ORORO returned invalid quality versions")
    for quality, path in versions.items():
        if urlparse(str(path)).path == download_path:
            return str(quality)
    numeric = []
    for quality in versions:
        try:
            numeric.append((quality_height(str(quality)), str(quality)))
        except DownloadError:
            pass
    if numeric:
        return max(numeric)[1]
    return str(detail.get("resolution") or fallback)


def hls_attributes(line: str) -> dict[str, str]:
    _, _, text = line.partition(":")
    result: dict[str, str] = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text):
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        result[match.group(1)] = value
    return result


def parse_hls_variants(master_url: str, text: str) -> list[HLSVariant]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants: list[HLSVariant] = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        uri = next((item for item in lines[index + 1 :] if not item.startswith("#")), None)
        if not uri:
            raise DownloadError("ORORO returned an HLS variant without a URL")
        attributes = hls_attributes(line)
        quality_match = re.search(r"__(\d+)p\.mp4_video\.m3u8", uri)
        if quality_match:
            quality = f"{quality_match.group(1)}p"
        else:
            resolution = attributes.get("RESOLUTION", "")
            width_text = resolution.partition("x")[0]
            try:
                width = int(width_text)
            except ValueError as exc:
                raise DownloadError("ORORO returned an HLS variant without a resolution") from exc
            quality = quality_from_dimensions(width)
        variants.append(
            HLSVariant(
                program_id=len(variants),
                quality=quality,
                bandwidth=int(attributes.get("BANDWIDTH") or 0),
                url=urljoin(master_url, uri),
            )
        )
    if not variants:
        raise DownloadError("ORORO returned an HLS playlist without quality variants")
    return variants


def hls_duration(text: str) -> float:
    duration = 0.0
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            value = line.partition(":")[2].partition(",")[0]
            duration += float(value)
    return duration


def quality_from_dimensions(width: int) -> str:
    if width >= 3200:
        return "2160p"
    if width >= 2200:
        return "1440p"
    if width >= 1600:
        return "1080p"
    if width >= 1100:
        return "720p"
    if width >= 750:
        return "480p"
    if width >= 550:
        return "360p"
    return "240p"


def local_quality(path: Path) -> str:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise FatalError("ffprobe is required to inspect an existing file before upgrading it")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        width = int(result.stdout.strip())
    except ValueError as exc:
        raise DownloadError(f"could not read video resolution from existing file: {path}") from exc
    return quality_from_dimensions(width)


def select_source(detail: dict, fallback: str, max_quality: int | None) -> MediaSource:
    direct_url = detail.get("download_url")
    if not direct_url:
        raise DownloadError("the subscription did not provide a download URL")
    direct_quality = chosen_quality(detail, fallback)
    try:
        direct_height = quality_height(direct_quality)
    except DownloadError:
        direct_height = max(QUALITY_LEVELS)

    if max_quality is None or direct_height <= max_quality:
        size, etag, modified = probe_source(str(direct_url))
        return MediaSource(
            mode="direct",
            url=str(direct_url),
            quality=direct_quality,
            size=size,
            exact_size=True,
            etag=etag,
            last_modified=modified,
        )

    master_url = detail.get("url")
    if not master_url:
        raise DownloadError(f"ORORO did not provide a stream for quality below {direct_quality}")
    with request_url(str(master_url)) as response:
        master = response.read().decode("utf-8-sig")
    variants = parse_hls_variants(str(master_url), master)
    eligible = [variant for variant in variants if quality_height(variant.quality) <= max_quality]
    if not eligible:
        available = ", ".join(variant.quality for variant in variants)
        raise DownloadError(f"no quality at or below {max_quality}p; available: {available}")
    selected = max(eligible, key=lambda variant: quality_height(variant.quality))
    with request_url(selected.url) as response:
        media_playlist = response.read().decode("utf-8-sig")
    duration = hls_duration(media_playlist)
    estimate = int(selected.bandwidth * duration / 8) if selected.bandwidth and duration else None
    return MediaSource(
        mode="hls",
        url=str(master_url),
        quality=selected.quality,
        size=estimate,
        exact_size=False,
        program_id=selected.program_id,
    )


def existing_action(path: Path, source: MediaSource) -> tuple[str, str | None]:
    if not path.exists():
        return "download", None
    if source.mode == "direct" and source.size is not None and path.stat().st_size == source.size:
        return "skip", source.quality
    current = local_quality(path)
    current_height = quality_height(current)
    desired_height = quality_height(source.quality)
    if current_height > desired_height:
        return "skip", current
    if current_height == desired_height:
        if source.exact_size and source.size is not None:
            raise DownloadError(
                f"existing {current} file has the wrong size: {path}; move it aside and retry"
            )
        return "skip", current
    return "upgrade", current


def partial_path(destination: Path, mode: str = "direct") -> Path:
    return destination.with_suffix(destination.suffix + ".part") if mode == "direct" else destination.with_name(f".{destination.name}.part")


def source_identity(source: MediaSource) -> dict:
    parsed = urlparse(source.url)
    # With validators, signed query strings may rotate without changing the file.
    resource = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    strong_etag = source.etag and not source.etag.startswith("W/")
    if not strong_etag and not source.last_modified and parsed.query:
        resource += f"?{parsed.query}"
    return {"source": hashlib.sha256(resource.encode()).hexdigest(), "quality": source.quality, "size": source.size,
            "etag": source.etag, "last_modified": source.last_modified}


def resume_status(destination: Path, source: MediaSource, restart: bool = False) -> tuple[int, int, list[str]]:
    partial = partial_path(destination, source.mode)
    size = partial.stat().st_size if partial.exists() else 0
    if not size or restart or source.mode != "direct":
        return 0, size, []
    metadata = partial.with_suffix(partial.suffix + ".json")
    try:
        saved = json.loads(metadata.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, UnicodeError):
        saved = None
    if saved != source_identity(source) or (source.size is not None and size > source.size):
        return 0, size, ["partial file has no matching source metadata; it will restart"]
    return size, size, []


def save_resume_metadata(path: Path, source: MediaSource) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".ororo-resume.", dir=path.parent, text=True)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(source_identity(source), output)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def target_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the inode after unlocking: unlinking it permits two simultaneous locks.
    lock_path = path.with_name(f".{path.name}.ororo.lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DownloadError(f"another ororo process is downloading {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def progress_line(
    label: str,
    done: int,
    total: int | None,
    started: float,
    initial: int = 0,
) -> str:
    elapsed = max(time.monotonic() - started, 0.001)
    speed = max(done - initial, 0) / elapsed
    if total:
        percent = min(100, int(done * 100 / total))
        return f"\r{label}: {percent:3d}%  {human_size(done)} / {human_size(total)}  {human_size(int(speed))}/s"
    return f"\r{label}: {human_size(done)}  {human_size(int(speed))}/s"


def download_video(
    url: str, destination: Path, expected_size: int | None, label: str,
    *, source: MediaSource | None = None, progress: bool | None = None, restart: bool = False,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = source or MediaSource("direct", url, "unknown", expected_size, True)
    partial = partial_path(destination)
    metadata = partial.with_suffix(partial.suffix + ".json")
    progress = sys.stderr.isatty() if progress is None else progress

    if destination.exists():
        current = destination.stat().st_size
        if expected_size is None or current == expected_size:
            print(f"skip video: {destination}")
            return
        raise DownloadError(
            f"existing file has the wrong size: {destination} "
            f"({human_size(current)} instead of {human_size(expected_size)})"
        )

    start, _, warnings = resume_status(destination, source, restart)
    for warning in warnings:
        print(f"warning: {label}: {warning}", file=sys.stderr)
    if partial.exists() and expected_size is not None and start == expected_size:
        os.replace(partial, destination)
        metadata.unlink(missing_ok=True)
        print(f"completed existing partial file: {destination}")
        return
    validator = source.etag if source.etag and not source.etag.startswith("W/") else source.last_modified
    kwargs = {"start": start if start else None}
    if start and validator:
        kwargs["validator"] = validator
    response = request_url(url, **kwargs)
    segment_size = None
    try:
        status = response.getcode()
        if status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("Content-Range", ""))
            if not match:
                raise DownloadError("server returned 206 without a valid Content-Range")
            first, last = int(match[1]), int(match[2])
            total = int(match[3]) if match[3] != "*" else None
            if first != start or last < first or (total is not None and last >= total):
                raise DownloadError("server returned the wrong Content-Range; partial file was preserved")
            if expected_size is not None and total is not None and total != expected_size:
                raise TransferError("remote file size changed; refreshing the download source")
            if expected_size is not None and last >= expected_size:
                raise DownloadError("server returned a range beyond the expected file size")
            if expected_size is None:
                expected_size = total
            segment_size = last - first + 1
        elif status == 200:
            # A server may ignore Range or reject If-Range; use its full response.
            start = 0
            length = response.headers.get("Content-Length")
            if expected_size is not None and length is not None and int(length) != expected_size:
                raise TransferError("remote file size changed; refreshing the download source")
        else:
            raise DownloadError(f"unexpected download HTTP status: {status}")
        etag = response.headers.get("ETag")
        if source.etag and etag and source.etag != etag:
            raise TransferError("remote file validator changed; refreshing the download source")
        modified = response.headers.get("Last-Modified")
        if source.last_modified and modified and source.last_modified != modified:
            raise TransferError("remote file modification time changed; refreshing the download source")
    except BaseException:
        response.close()
        raise

    mode = "ab" if start else "wb"
    done = start
    started = time.monotonic()
    last_progress = 0.0
    try:
        with response, partial.open(mode) as output:
            save_resume_metadata(metadata, source)
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                if segment_size is not None and done - start + len(chunk) > segment_size:
                    raise DownloadError("server sent more bytes than its Content-Range; partial file was preserved")
                if expected_size is not None and done + len(chunk) > expected_size:
                    raise DownloadError("server sent more bytes than the expected file size; partial file was preserved")
                output.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress and (now - last_progress >= 0.2 or done == expected_size):
                    print(progress_line(label, done, expected_size, started, start), end="", file=sys.stderr, flush=True)
                    last_progress = now
    finally:
        if progress:
            print(file=sys.stderr)

    if segment_size is not None and done - start != segment_size:
        raise TransferError(f"incomplete HTTP range for {label}")
    if expected_size is not None and done != expected_size:
        raise TransferError(
            f"incomplete download for {label}: got {human_size(done)}, expected {human_size(expected_size)}"
        )
    os.replace(partial, destination)
    metadata.unlink(missing_ok=True)
    print(f"saved video: {destination}")


def subtitle_selection(value: str) -> set[str] | None:
    value = value.strip().casefold()
    if value == "all":
        return None
    if value in {"none", ""}:
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def selected_subtitles(detail: dict, selection: set[str] | None) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for subtitle in object_list(detail, "subtitles"):
        language = str(subtitle.get("lang") or "und").casefold()
        url = subtitle.get("url")
        if not url or language in seen:
            continue
        if selection is not None and language not in selection:
            continue
        seen.add(language)
        result.append((language, str(url)))
    return result


def download_subtitle(url: str, destination: Path) -> None:
    if destination.exists():
        print(f"skip subtitle: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    with request_url(url) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output, CHUNK_SIZE)
    os.replace(partial, destination)
    print(f"saved subtitle: {destination}")


def download_hls(source: MediaSource, destination: Path, label: str, *, progress: bool | None = None) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise DownloadError("ffmpeg is required when --max-quality selects a smaller version")
    if source.program_id is None:
        raise DownloadError("missing HLS program ID")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_path(destination, "hls")
    if partial.exists():
        partial.unlink()
    print(f"downloading {source.quality} stream with ffmpeg; interrupted HLS downloads restart")
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stats" if (sys.stderr.isatty() if progress is None else progress) else "-nostats",
        "-y",
        "-extension_picky",
        "0",
        "-i",
        source.url,
        "-map",
        f"0:p:{source.program_id}:v:0",
        "-map",
        f"0:p:{source.program_id}:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(partial),
    ]
    result = subprocess.run(command)
    if result.returncode != 0:
        raise DownloadError(f"ffmpeg failed for {label} with exit code {result.returncode}")
    os.replace(partial, destination)
    print(f"saved video: {destination}")


def transient_error(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code in {408, 425, 429} or 500 <= exc.code < 600
    if isinstance(exc, URLError):
        return not isinstance(exc.reason, OSError) or exc.reason.errno not in LOCAL_ERRNOS
    if isinstance(exc, OSError):
        return exc.errno not in LOCAL_ERRNOS
    return isinstance(exc, (TransferError, http.client.HTTPException))


def retry(operation: Callable[[], T], label: str, attempts: int = 3) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (HTTPError, URLError, OSError, TransferError, http.client.HTTPException) as exc:
            if not transient_error(exc) or attempt == attempts:
                raise
            wait = 2 ** (attempt - 1)
            print(f"{label}: {exc}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)


def size_text(source: MediaSource) -> str:
    prefix = "" if source.exact_size else "~"
    return prefix + human_size(source.size)


def plan_target(
    api: OroroAPI, target: Target, subs: set[str] | None, max_quality: int | None,
    *, restart: bool = False,
) -> PlanItem:
    detail = api.detail(target.kind, target.media_id)
    source = select_source(detail, target.resolution, max_quality)
    action, current = existing_action(target.video_path, source)
    destination = target.video_path
    if action == "upgrade":
        destination = target.directory / f".{target.stem}.{source.quality}.upgrade.mp4"
    selected = tuple(selected_subtitles(detail, subs))
    available = {str(item.get("lang") or "") for item in object_list(detail, "subtitles")}
    warnings = []
    if subs:
        missing = sorted(subs - available)
        if missing:
            warnings.append(f"requested subtitles are unavailable: {', '.join(missing)}")
    existing_bytes = target.video_path.stat().st_size if target.video_path.exists() else 0
    resumed, partial_bytes = 0, 0
    download_bytes, additional = 0, 0
    if action != "skip":
        if destination != target.video_path and destination.exists() and source.size == destination.stat().st_size:
            # A crash can leave a finished replacement just before the final rename.
            resumed, partial_bytes, resume_warnings = source.size, source.size, []
        else:
            resumed, partial_bytes, resume_warnings = resume_status(destination, source, restart)
        warnings.extend(resume_warnings)
        download_bytes = max(0, source.size - resumed) if source.size is not None else None
        additional = max(0, source.size - partial_bytes) if source.size is not None else None
        if source.mode == "hls" and not shutil.which("ffmpeg"):
            raise FatalError("ffmpeg is required when --max-quality selects an HLS stream")
    return PlanItem(target, source, action, current, destination, resumed, partial_bytes,
                    download_bytes, additional, existing_bytes, selected, tuple(warnings))


def item_json(item: PlanItem) -> dict:
    return {
        "kind": item.target.kind, "id": item.target.media_id, "label": item.target.label,
        "path": str(item.target.video_path), "quality": item.source.quality,
        "current_quality": item.current_quality, "action": item.action, "mode": item.source.mode,
        "size_bytes": item.existing_bytes if item.action == "skip" else item.source.size,
        "download_bytes": item.download_bytes, "additional_space_bytes": item.additional_space_bytes,
        "resume_bytes": item.resume_bytes, "estimated": not item.source.exact_size,
        "subtitles": [language for language, _ in item.subtitles], "warnings": list(item.warnings),
    }


def plan_summary(items: list[PlanItem]) -> dict:
    final, download, peak, growth = 0, 0, 0, 0
    known_final, known_download, known_space = True, True, True
    for item in items:
        size = item.existing_bytes if item.action == "skip" else item.source.size
        if size is None:
            known_final = False
        else:
            final += size
        if item.download_bytes is None:
            known_download = False
        else:
            download += item.download_bytes
        if item.action != "skip":
            if item.source.size is None:
                known_space = False
            else:
                peak = max(peak, growth + item.additional_space_bytes)
                growth += item.source.size - item.existing_bytes - item.partial_bytes
    return {
        "videos": len(items), "final_size_bytes": final if known_final else None,
        "download_bytes": download if known_download else None,
        "additional_space_bytes": max(0, peak) if known_space else None,
        "estimated": any(not item.source.exact_size and item.action != "skip" for item in items),
    }


def record_batch_error(exc: Exception, label: str, keep_going: bool, errors: list[dict]) -> None:
    if isinstance(exc, (AuthenticationError, FatalError)):
        raise exc
    if isinstance(exc, OSError) and exc.errno in LOCAL_ERRNOS:
        raise FatalError(str(exc)) from exc
    if not keep_going:
        raise exc
    errors.append({"label": label, "error": str(exc)})
    print(f"error: {label}: {exc}", file=sys.stderr)


def print_summary(summary: dict, errors: list[dict]) -> None:
    prefix = "~" if summary["estimated"] else ""
    print(f"\n{summary['videos']} video(s)")
    print(f"estimated final archive size: {prefix}{human_size(summary['final_size_bytes'])}")
    print(f"data to download this run: {prefix}{human_size(summary['download_bytes'])}")
    print(f"additional free space needed: {prefix}{human_size(summary['additional_space_bytes'])}")
    if errors:
        print(f"failed items: {len(errors)}")
        for error in errors:
            print(f"  {error['label']}: {error['error']}")


def dry_run(
    api: OroroAPI, targets: Iterable[Target], subs: set[str] | None, max_quality: int | None,
    *, keep_going: bool = False, restart: bool = False, json_output: bool = False,
    errors: list[dict] | None = None,
) -> dict:
    items, errors = [], list(errors or [])
    for target in targets:
        try:
            item = retry(lambda: plan_target(api, target, subs, max_quality, restart=restart), target.label)
            items.append(item)
        except (DownloadError, OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
            record_batch_error(exc, target.label, keep_going, errors)
            continue
        if not json_output:
            action = f"already {item.current_quality}" if item.action == "skip" else item.action
            languages = ",".join(language for language, _ in item.subtitles) or "none"
            print(f"{target.video_path}  [{item.source.quality}, {size_text(item.source)}, {action}, subtitles: {languages}]")
            for warning in item.warnings:
                print(f"warning: {target.label}: {warning}", file=sys.stderr)
    summary = plan_summary(items)
    if not json_output:
        print_summary(summary, errors)
    return {"items": [item_json(item) for item in items], "summary": summary, "errors": errors}


def download_all(
    api: OroroAPI, targets: list[Target], subs: set[str] | None, delay: float,
    max_quality: int | None, *, keep_going: bool = False, restart: bool = False,
    progress: bool | None = None, errors: list[dict] | None = None,
) -> dict:
    items, errors = [], list(errors or [])
    for position, target in enumerate(targets, start=1):
        print(f"\n[{position}/{len(targets)}] {target.label}")
        restart_attempt = restart

        def operation() -> PlanItem:
            nonlocal restart_attempt
            # Refresh inside the lock and on retry: signed URLs and local files can change.
            item = plan_target(api, target, subs, max_quality, restart=restart_attempt)
            print(f"quality: {item.source.quality}; size: {size_text(item.source)}")
            for warning in item.warnings:
                print(f"warning: {target.label}: {warning}", file=sys.stderr)
            if item.action == "skip":
                print(f"skip video: {target.video_path} (already {item.current_quality})")
            else:
                if item.source.exact_size and item.additional_space_bytes is not None:
                    free = shutil.disk_usage(target.directory).free
                    if item.additional_space_bytes > free:
                        raise FatalError(
                            f"not enough disk space for {target.label}: "
                            f"need {human_size(item.additional_space_bytes)}, free {human_size(free)}"
                        )
                if item.action == "upgrade":
                    print(f"upgrading {item.current_quality} -> {item.source.quality}; the old file stays until completion")
                if restart_attempt:
                    partial = partial_path(item.destination, item.source.mode)
                    if partial.exists():
                        with partial.open("wb"):
                            pass
                        partial.with_suffix(partial.suffix + ".json").unlink(missing_ok=True)
                    restart_attempt = False
                if item.source.mode == "direct":
                    download_video(item.source.url, item.destination, item.source.size, target.label,
                                   source=item.source, progress=progress)
                else:
                    download_hls(item.source, item.destination, target.label, progress=progress)
                if item.action == "upgrade":
                    os.replace(item.destination, target.video_path)
                    print(f"upgraded video: {target.video_path}")
            for language, subtitle_url in item.subtitles:
                path = target.directory / f"{target.stem}.{language}.vtt"
                download_subtitle(subtitle_url, path)
            return item

        try:
            with target_lock(target.video_path):
                items.append(retry(operation, target.label))
        except (DownloadError, OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
            record_batch_error(exc, target.label, keep_going, errors)
        if delay and position < len(targets):
            time.sleep(delay)
    summary = plan_summary(items)
    print(f"\ncompleted: {len(items)}; failed: {len(errors)}")
    for error in errors:
        print(f"  {error['label']}: {error['error']}")
    return {"items": [item_json(item) for item in items], "summary": summary, "errors": errors}


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DownloadError(f"{message}; see ororo --help")


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog="ororo", description="Download ORORO movies and shows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Commands:
  ororo URL ...             download movies or shows
  ororo login               verify and save credentials
  ororo search QUERY        find movies and shows by title
  ororo info URL ...        show descriptions, seasons and episodes

Examples:
  ororo search "the martian" --type movies --json
  ororo URL --dry-run --max-quality 1080p
  ororo -i list.txt -o /srv/media --keep-going
""",
    )
    parser.add_argument("urls", nargs="*", metavar="URL/COMMAND", help="page URLs, or login/search/info and their arguments")
    parser.add_argument("-i", "--input-file", type=Path, metavar="FILE", help="read one ORORO URL per line")
    parser.add_argument("-o", "--output", type=Path, help="archive directory (default: config or current directory)")
    parser.add_argument("--user", help="ORORO login email (overrides environment and saved login)")
    parser.add_argument("--config", type=Path, metavar="FILE", help="settings JSON (default: $XDG_CONFIG_HOME/ororo/config.json)")
    parser.add_argument("--credentials-file", type=Path, metavar="FILE", help="read/save credentials at this path")
    parser.add_argument("--season", action="append", help="show seasons, e.g. 1,3-4")
    parser.add_argument("--episode", action="append", help="episode numbers within selected seasons")
    parser.add_argument("--max-quality", metavar="QUALITY", help="quality ceiling: 240p..2160p or best (default: best)")
    parser.add_argument("--subs", help="subtitle languages, all or none (default: en,ru)")
    parser.add_argument("--delay", type=float, help="seconds between videos (default: 1)")
    parser.add_argument("--dry-run", action="store_true", help="preview files, remaining traffic and additional disk space")
    parser.add_argument("--keep-going", action="store_true", help="continue after individual failures; exit 1 if any failed")
    parser.add_argument("--restart", action="store_true", help="restart partial downloads instead of resuming")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt for credentials")
    parser.add_argument("--no-progress", action="store_true", help="disable progress animation (automatic outside a terminal)")
    parser.add_argument("--json", action="store_true", help="write one JSON result; never prompt or animate progress")
    parser.add_argument("--type", choices=("all", "movies", "shows"), default="all", help="catalog to search (default: all)")
    parser.add_argument("--version", action="version", version=f"ororo {VERSION}")
    return parser


def apply_settings(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    for key, default in (("output", Path.cwd()), ("max_quality", "best"), ("subs", "en,ru"), ("delay", 1.0)):
        if getattr(args, key) is None:
            setattr(args, key, config.get(key, default))
    args.output = Path(args.output).expanduser()
    args.delay = float(args.delay)
    if not math.isfinite(args.delay) or args.delay < 0:
        raise DownloadError("--delay must be a finite non-negative number")


def validate_command_options(args: argparse.Namespace, command: str) -> None:
    if command != "download":
        download_only = ("input_file", "output", "max_quality", "subs", "delay", "dry_run", "restart", "no_progress")
        for key in download_only:
            value = getattr(args, key)
            if value is not None and value is not False:
                # info can use the same input list as downloads.
                if command == "info" and key == "input_file":
                    continue
                raise DownloadError(f"--{key.replace('_', '-')} is only available for downloads")
    if command in {"login", "search"} and (args.season or args.episode or args.keep_going):
        raise DownloadError("season/episode filters and --keep-going require info or a download")
    if command != "search" and args.type != "all":
        raise DownloadError("--type is only available for search")


def print_info(item: dict) -> None:
    print(f"{item.get('name') or 'Untitled'} ({item.get('year') or 'unknown year'})")
    print(item["url"])
    if item.get("resolution"):
        print(f"quality: {item['resolution']}")
    if item.get("description"):
        print(str(item["description"]))
    for episode in item.get("episodes", []):
        print(f"  S{episode['season']:02d}E{episode['episode']:02d}  {episode.get('name') or ''}")
    if "subtitles" in item:
        print(f"subtitles: {','.join(item['subtitles']) or 'none'}")
    print()


def run_command(
    args: argparse.Namespace, command: str, pages: list[Page], seasons: set[int] | None,
    episodes: set[int] | None, subs: set[str] | None, max_quality: int | None,
) -> dict:
    credentials = resolve_credentials(args, login=command == "login")
    api = OroroAPI(credentials.user, credentials.password)
    if command == "login" or credentials.prompted:
        # Validate before writing a password to disk.
        api.catalog(pages[0].kind if pages else "movies")
        save_credentials(credentials.path, credentials.user, credentials.password)
        print(f"saved credentials: {credentials.path}", file=sys.stderr)
    if command == "login":
        if not args.json:
            print("ORORO login verified.")
        return {"credentials_file": str(credentials.path)}
    if command == "search":
        items = search_catalog(api, " ".join(args.urls), args.type)
        if not args.json:
            for item in items:
                print(f"{item['name']} ({item['year'] or '?'}) [{item['kind']}]\n  {item['url']}")
            print(f"\n{len(items)} result(s)")
        return {"items": items}
    if command == "info":
        items, errors = [], []
        for page in pages:
            try:
                item = page_info(api, page, seasons, episodes)
                items.append(item)
                if not args.json:
                    print_info(item)
            except (DownloadError, OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
                record_batch_error(exc, page.url, args.keep_going, errors)
        return {"items": items, "errors": errors}
    targets, errors = [], []
    for page in pages:
        try:
            targets.extend(targets_for_page(api, page, args.output, seasons, episodes))
        except (DownloadError, OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
            record_batch_error(exc, page.url, args.keep_going, errors)
    targets = list(dict.fromkeys(targets))
    if args.dry_run:
        return dry_run(api, targets, subs, max_quality, keep_going=args.keep_going,
                       restart=args.restart, json_output=args.json, errors=errors)
    with redirect_stdout(sys.stderr if args.json else sys.stdout):
        return download_all(api, targets, subs, args.delay, max_quality, keep_going=args.keep_going,
                            restart=args.restart, progress=not args.no_progress and not args.json and sys.stderr.isatty(),
                            errors=errors)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in raw_args
    command = "download"
    try:
        args = build_parser().parse_intermixed_args(raw_args)
        if args.urls and args.urls[0] in {"login", "search", "info"}:
            command = args.urls.pop(0)
        validate_command_options(args, command)
        if command == "login":
            if args.urls or args.input_file:
                raise DownloadError("ororo login does not accept URLs or an input file")
            pages = []
        elif command == "search":
            if not " ".join(args.urls).strip() or args.input_file:
                raise DownloadError("usage: ororo search QUERY [--type movies|shows|all]")
            pages = []
        else:
            pages = pages_from_inputs(args.urls, args.input_file)
        apply_settings(args)
        seasons = parse_number_set(args.season)
        episodes = parse_number_set(args.episode)
        subs = subtitle_selection(args.subs)
        max_quality = parse_max_quality(args.max_quality)
        result = run_command(args, command, pages, seasons, episodes, subs, max_quality)
        ok = not result.get("errors")
        if args.json:
            print(json.dumps({"command": command, "ok": ok, **result}, ensure_ascii=False))
        return 0 if ok else 1
    except (DownloadError, OSError, ValueError, UnicodeError, http.client.HTTPException, EOFError) as exc:
        message = str(exc) or "input ended unexpectedly; run ororo login in a terminal"
        if isinstance(exc, AuthenticationError) and "ororo login" not in message:
            message += "; run ororo login to update credentials"
        if json_output:
            print(json.dumps({"command": command, "ok": False, "error": message}, ensure_ascii=False))
        print(f"error: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if json_output:
            print(json.dumps({"command": command, "ok": False, "error": "interrupted"}))
        print("\ninterrupted; direct MP4 downloads can resume, HLS downloads restart", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
