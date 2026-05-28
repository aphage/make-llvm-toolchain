from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_REQUEST_HEADERS = {
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "User-Agent": "make-llvm-toolchain/0.1",
}
_REQUEST_TIMEOUT_SECONDS = 30

_LLVM_RELEASES_API = "https://api.github.com/repos/llvm/llvm-project/releases/latest"
_LINUX_RELEASES_URL = "https://www.kernel.org/releases.json"
_GLIBC_RELEASES_URL = "https://ftp.gnu.org/gnu/glibc/"
_GCC_RELEASES_URL = "https://ftp.gnu.org/gnu/gcc/"
_MUSL_RELEASES_URLS = (
    "https://musl.libc.org/releases.html",
    "https://musl.libc.org/",
)


class ReleaseVersionError(RuntimeError):
    """Raised when a latest-release lookup fails."""


@dataclass(frozen=True)
class ResolvedComponentVersions:
    linux_version: str
    glibc_version: str
    libstdcxx_version: str
    musl_version: str


def resolve_component_versions(
    *,
    linux_version: str | None,
    glibc_version: str | None,
    libstdcxx_version: str | None,
    musl_version: str | None,
) -> ResolvedComponentVersions:
    return ResolvedComponentVersions(
        linux_version=_resolve_requested_version(
            linux_version,
            "--linux-version",
            resolve_latest_linux_version,
        ),
        glibc_version=_resolve_requested_version(
            glibc_version,
            "--glibc-version",
            resolve_latest_glibc_version,
        ),
        libstdcxx_version=_resolve_requested_version(
            libstdcxx_version,
            "--libstdcxx-version",
            resolve_latest_libstdcxx_version,
        ),
        musl_version=_resolve_requested_version(
            musl_version,
            "--musl-version",
            resolve_latest_musl_version,
        ),
    )


def llvm_git_ref_for_version(version: str) -> str:
    normalized = version.strip()
    if not normalized:
        raise ReleaseVersionError("--llvm-version must not be empty.")
    if normalized.startswith("llvmorg-"):
        normalized = normalized.removeprefix("llvmorg-")
    if not re.fullmatch(r"\d+\.\d+\.\d+", normalized):
        raise ReleaseVersionError(
            "--llvm-version must look like X.Y.Z or llvmorg-X.Y.Z."
        )
    return f"llvmorg-{normalized}"


def resolve_latest_llvm_release_version() -> str:
    payload = _fetch_json(_LLVM_RELEASES_API)
    tag_name = str(payload.get("tag_name", "")).strip()
    if not tag_name:
        raise ReleaseVersionError("Could not determine the latest published LLVM release.")
    if tag_name.startswith("llvmorg-"):
        return tag_name.removeprefix("llvmorg-")
    return tag_name


def resolve_latest_linux_version() -> str:
    payload = _fetch_json(_LINUX_RELEASES_URL)
    releases = payload.get("releases")
    if not isinstance(releases, list):
        raise ReleaseVersionError("Could not determine the latest published Linux release.")
    stable_versions = [
        str(entry.get("version", "")).strip()
        for entry in releases
        if isinstance(entry, dict) and entry.get("moniker") == "stable"
    ]
    return _latest_version(stable_versions, "Linux kernel")


def resolve_latest_glibc_version() -> str:
    listing = _fetch_text(_GLIBC_RELEASES_URL)
    return _latest_version_from_matches(
        listing,
        re.compile(r"glibc-(\d+\.\d+(?:\.\d+)?)\.tar\.xz"),
        "glibc",
    )


def resolve_latest_libstdcxx_version() -> str:
    listing = _fetch_text(_GCC_RELEASES_URL)
    return _latest_version_from_matches(
        listing,
        re.compile(r"gcc-(\d+\.\d+\.\d+)/"),
        "GCC/libstdc++",
    )


def resolve_latest_musl_version() -> str:
    last_error: ReleaseVersionError | None = None
    for url in _MUSL_RELEASES_URLS:
        try:
            listing = _fetch_text(url)
            return _latest_version_from_matches(
                listing,
                re.compile(r"musl-(\d+\.\d+\.\d+)\.tar\.gz"),
                "musl",
            )
        except ReleaseVersionError as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise ReleaseVersionError("Could not determine the latest published musl release.")


def _resolve_requested_version(
    value: str | None,
    option_name: str,
    latest_resolver: callable[[], str],
) -> str:
    if value is None:
        return latest_resolver()
    normalized = value.strip()
    if not normalized:
        raise ReleaseVersionError(f"{option_name} must not be empty.")
    if normalized.lower() == "latest":
        return latest_resolver()
    return normalized


def _latest_version_from_matches(text: str, pattern: re.Pattern[str], label: str) -> str:
    return _latest_version((match.group(1) for match in pattern.finditer(text)), label)


def _latest_version(versions: list[str] | tuple[str, ...] | set[str] | object, label: str) -> str:
    candidates = [version for version in versions if str(version).strip()]
    if not candidates:
        raise ReleaseVersionError(f"Could not determine the latest published {label} release.")
    return max(candidates, key=_version_key)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _fetch_json(url: str) -> dict[str, object]:
    payload = _fetch_text(url)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ReleaseVersionError(f"Failed to parse release metadata from {url}.") from error
    if not isinstance(parsed, dict):
        raise ReleaseVersionError(f"Unexpected release metadata shape from {url}.")
    return parsed


def _fetch_text(url: str) -> str:
    request = Request(url, headers=_REQUEST_HEADERS)
    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError, OSError) as error:
        raise ReleaseVersionError(f"Failed to fetch release metadata from {url}: {error}") from error