#!/usr/bin/env python3
"""craft-bridge: mirror an upstream project's GitHub releases under the craft-app.json contract.

A bridge repo holds a `bridge.toml`. This script reads it, finds the upstream project's latest
release, downloads the builds it publishes (never rebuilding anything), renames them to
`<id>-<version>-<os>-<arch>[-portable].<ext>`, repacks an archive only when its layout would
stop Craft Unofficial Launcher from installing it, adds `craft-app.json`, the icon, upstream's
LICENSE/NOTICE files and `SHA256SUMS.txt`, checks the whole release against a port of the
launcher's own rules, and publishes it with `gh release create`.

Python 3.11+ standard library only. Everything downloaded is treated as untrusted data: it is
size-capped, checksum-verified, and only ever read (archives are listed or copied entry by entry,
never extracted to disk and never run).

Usage:
    python -I bridge.py [--config bridge.toml] [--dry-run] [--force] [--tag TAG] [--backfill N]
    python -I bridge.py --check DIR --tag TAG      # validate a folder as a release of this bridge

Exit codes: 0 done (or nothing to do), 1 the release couldn't be built or breaks the contract,
2 bad arguments/config or a network failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

VERSION = "1.0.0"
USER_AGENT = f"craft-bridge/{VERSION}"
API = "https://api.github.com"

# ---------------------------------------------------------------------------------------------
# Port of crates/descriptor (craft-unofficial-launcher). Keep in step with the Rust code: the
# launcher and craft-release-check run that code, and it wins if the two ever disagree.
# ---------------------------------------------------------------------------------------------

DESCRIPTOR_FILE = "craft-app.json"
SUMS_FILE = "SHA256SUMS.txt"
SCHEMA = 1
MAX_DESCRIPTOR_BYTES = 16 * 1024
MAX_ICON_BYTES = 256 * 1024
MAX_ICON_EDGE = 512
MAX_SUMS_BYTES = 1024 * 1024
MAX_LIST = 8

SUFFIXES = ("-portable.zip", ".tar.gz", ".msi", ".dmg", ".AppImage", ".deb", ".rpm", ".pkg.tar.zst", ".flatpak")
TOKENS = (
    "windows-x64", "windows-x86", "windows-arm64",
    "macos-universal", "macos-x86_64", "macos-aarch64", "macos-arm64",
    "linux-x86_64", "linux-aarch64", "linux-riscv64",
    "freebsd-x86_64", "freebsd-aarch64",
)

RESERVED_IDS = (
    "photocraft", "vectorcraft", "filmcraft", "lightcraft", "pdfcraft", "printcraft", "effectcraft",
    "designcraft", "figcraft", "wordcraft", "soundcraft", "deckcraft", "cadcraft", "gridcraft",
    "artcraft", "artcraft-launcher", "craft-launcher", "craft-unofficial-launcher",
)
BUNDLE_PREFIXES = ("ai.storyteller.",)
RESERVED_MSI_PUBLISHERS = ("learning machines llc", "learning machines", "artcraft", "storyteller")
RESERVED_APPLE_TEAMS = ("DJ6XS33FX8",)
RESERVED_OWNERS = ("storytold",)
RESERVED_PUBLISHERS = ("artcraft", "storyteller", "storyteller.ai", "learning machines llc")
LAUNCHER_ID = "craft-unofficial-launcher"
LAUNCHER_REPO = "YalKouassi/craft-unofficial-launcher"

# Rust's `char::is_whitespace` (Unicode White_Space), which `str::trim` strips.
_RUST_WS = frozenset(
    chr(c) for c in [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
)
_ASCII_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz")
_ASCII_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_DIGIT = frozenset("0123456789")
_ASCII_ALNUM = _ASCII_LOWER | _ASCII_UPPER | _ASCII_DIGIT
_HEX = frozenset("0123456789abcdefABCDEF")


def rust_trim(s: str) -> str:
    """`str::trim` (Unicode White_Space only; Python's `strip` also drops some control chars)."""
    start, end = 0, len(s)
    while start < end and s[start] in _RUST_WS:
        start += 1
    while end > start and s[end - 1] in _RUST_WS:
        end -= 1
    return s[start:end]


def ascii_lower(s: str) -> str:
    """`to_ascii_lowercase`: only A-Z change (Python's `lower` folds e.g. the Kelvin sign to k)."""
    return "".join(chr(ord(c) + 32) if c in _ASCII_UPPER else c for c in s)


def _is_control(c: str) -> bool:
    return unicodedata.category(c) == "Cc"


def _is_bidi_control(c: str) -> bool:
    o = ord(c)
    return o in (0x200E, 0x200F) or 0x202A <= o <= 0x202E or 0x2066 <= o <= 0x2069


def reserved_id(s: str) -> bool:
    return ascii_lower(s) in RESERVED_IDS


def reserved_id_for_repo(id_: str, repo: str) -> bool:
    return reserved_id(id_) and not (id_ == LAUNCHER_ID and ascii_lower(repo) == ascii_lower(LAUNCHER_REPO))


def reserved_bundle(b: str) -> bool:
    lower = ascii_lower(b)
    return any(lower.startswith(p) or lower == p.rstrip(".") for p in BUNDLE_PREFIXES)


def reserved_msi_publisher(p: str) -> bool:
    return ascii_lower(rust_trim(p)) in RESERVED_MSI_PUBLISHERS


def reserved_apple_team(t: str) -> bool:
    return any(ascii_lower(x) == ascii_lower(rust_trim(t)) for x in RESERVED_APPLE_TEAMS)


def reserved_publisher(p: str) -> bool:
    return ascii_lower(rust_trim(p)) in RESERVED_PUBLISHERS


def reserved_repo(repo: str) -> bool:
    owner = repo.split("/")[0]
    return any(ascii_lower(o) == ascii_lower(owner) for o in RESERVED_OWNERS)


def valid_id(s: str) -> bool:
    return (
        1 <= len(s.encode()) <= 40
        and all(c in _ASCII_LOWER or c in _ASCII_DIGIT or c == "-" for c in s)
        and not s.startswith("-")
        and not s.endswith("-")
        and "--" not in s
    )


def valid_text(s: str, max_chars: int) -> bool:
    return 1 <= len(s) <= max_chars and rust_trim(s) == s and not any(_is_control(c) or _is_bidi_control(c) for c in s)


def valid_repo(repo: str) -> bool:
    parts = repo.split("/")
    if len(parts) != 2:
        return False
    owner, name = parts
    owner_ok = 1 <= len(owner.encode()) <= 39 and all(c in _ASCII_ALNUM or c == "-" for c in owner) and not owner.startswith("-")
    name_ok = (
        1 <= len(name.encode()) <= 100
        and all(c in _ASCII_ALNUM or c in "-_." for c in name)
        and name not in (".", "..")
        and not name.endswith(".git")
    )
    return owner_ok and name_ok


def valid_bundle(b: str) -> bool:
    parts = b.split(".")
    return (
        len(b.encode()) <= 255
        and len(parts) >= 2
        and all(p and all(c in _ASCII_ALNUM or c in "-_" for c in p) and p[0] not in _ASCII_DIGIT for p in parts)
    )


def valid_https(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    rest = url[len("https://"):]
    host = re.split(r"[/?#]", rest, maxsplit=1)[0]
    return (
        len(url.encode()) <= 200
        and host != ""
        and "@" not in host
        and "\\" not in host
        and not any(_is_control(c) or c in _RUST_WS or c in "\"'<>`|^{}" for c in url)
    )


def parse_color(c: str) -> tuple[int, int, int] | None:
    if not c.startswith("#"):
        return None
    h = c[1:]
    if len(h) != 6 or not all(ch in _HEX for ch in h):
        return None
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def valid_file_name(name: str) -> bool:
    return (
        name != ""
        and len(name.encode()) <= 200
        and not name.startswith(".")
        and not any(_is_control(c) or c in '/\\:<>"|?*' for c in name)
    )


# Field name -> kind. "str" required text, "opt" optional text (absent or null), "list" a list of
# text (absent or [], never null), "u32" the schema number.
DESCRIPTOR_FIELDS = {
    "schema": "u32", "id": "str", "name": "str", "tagline": "str", "repo": "str", "publisher": "str",
    "upstream": "opt", "bundle_id": "str", "msi_publisher": "opt", "apple_team": "opt", "website": "opt",
    "color": "str", "icon": "opt", "former_ids": "list", "former_names": "list", "former_bundles": "list",
}
DESCRIPTOR_ORDER = list(DESCRIPTOR_FIELDS)


class _Dup(ValueError):
    pass


def _no_dups(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise _Dup(f"duplicate field `{k}`")
        seen[k] = v
    return seen


def _has_surrogate(v) -> bool:
    if isinstance(v, str):
        return any(0xD800 <= ord(c) <= 0xDFFF for c in v)
    if isinstance(v, list):
        return any(_has_surrogate(x) for x in v)
    if isinstance(v, dict):
        return any(_has_surrogate(k) or _has_surrogate(x) for k, x in v.items())
    return False


def _bad_constant(name):
    raise ValueError(f"{name} isn't JSON")


def parse_descriptor(data: bytes, repo: str) -> tuple[dict | None, list[str]]:
    """`Descriptor::parse`: (descriptor, []) or (None, problems). Shape errors stop at the first
    one (as serde does); the field checks then report every problem at once."""
    if len(data) > MAX_DESCRIPTOR_BYTES:
        return None, [f"{DESCRIPTOR_FILE} is {len(data)} bytes; the limit is {MAX_DESCRIPTOR_BYTES}"]
    bad = lambda why: (None, [f"{DESCRIPTOR_FILE} isn't valid: {why}"])  # noqa: E731
    try:
        text = data.decode("utf-8")
        d = json.loads(text, object_pairs_hook=_no_dups, parse_constant=_bad_constant)
    except (UnicodeDecodeError, ValueError) as e:
        return bad(e)
    if not isinstance(d, dict):
        return bad("expected a JSON object")
    if _has_surrogate(d):
        return bad("lone surrogate in a string")
    for k in d:
        if k not in DESCRIPTOR_FIELDS:
            return bad(f"unknown field `{k}`")
    for k, kind in DESCRIPTOR_FIELDS.items():
        v = d.get(k, None) if k in d else ...
        if kind == "u32":
            if v is ...:
                return bad(f"missing field `{k}`")
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0xFFFFFFFF:
                return bad(f"`{k}` must be a u32")
        elif kind == "str":
            if v is ...:
                return bad(f"missing field `{k}`")
            if not isinstance(v, str):
                return bad(f"`{k}` must be a string")
        elif kind == "opt":
            if v is ...:
                d[k] = None
            elif v is not None and not isinstance(v, str):
                return bad(f"`{k}` must be a string or null")
        else:
            if v is ...:
                d[k] = []
            elif not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return bad(f"`{k}` must be a list of strings")
    problems = check_descriptor(d, repo)
    return (None, problems) if problems else (d, [])


def check_descriptor(d: dict, repo: str) -> list[str]:
    """`Descriptor::check`."""
    p: list[str] = []

    def bad(ok: bool, msg: str):
        if not ok:
            p.append(msg)

    bad(d["schema"] == SCHEMA, f"schema {d['schema']} isn't supported (this launcher reads schema {SCHEMA})")
    bad(valid_id(d["id"]), f"id {d['id']!r} must be 1–40 lowercase letters, digits and single hyphens")
    bad(not reserved_id_for_repo(d["id"], d["repo"]), f"id {d['id']!r} belongs to an official app")
    bad(valid_text(d["name"], 40), f"name {d['name']!r} must be 1–40 characters of plain text")
    bad(not reserved_id(d["name"]), f"name {d['name']!r} belongs to an official app")
    bad(valid_text(d["tagline"], 80), "tagline must be 1–80 characters of plain text")
    bad(valid_repo(d["repo"]), f"repo {d['repo']!r} must be a GitHub owner/name")
    bad(ascii_lower(d["repo"]) == ascii_lower(repo), f"repo {d['repo']!r} doesn't match the repository it came from ({repo})")
    bad(not reserved_repo(d["repo"]), "an unofficial app can't live under an official GitHub account")
    bad(valid_text(d["publisher"], 60), "publisher must be 1–60 characters of plain text")
    bad(not reserved_publisher(d["publisher"]), f"publisher {d['publisher']!r} belongs to ArtCraft")
    if d.get("upstream") is not None:
        bad(valid_repo(d["upstream"]), f"upstream {d['upstream']!r} must be a GitHub owner/name")
    bad(valid_bundle(d["bundle_id"]), f"bundle_id {d['bundle_id']!r} must be reverse-DNS, like org.example.app")
    bad(not reserved_bundle(d["bundle_id"]), f"bundle_id {d['bundle_id']!r} is in ArtCraft's namespace")
    m = d.get("msi_publisher")
    if m is not None:
        bad(valid_text(m, 100), "msi_publisher must be 1–100 characters of plain text")
        bad(not reserved_msi_publisher(m), f"msi_publisher {m!r} belongs to ArtCraft")
    t = d.get("apple_team")
    if t is not None:
        bad(len(t.encode()) == 10 and all(c in _ASCII_UPPER or c in _ASCII_DIGIT for c in t), f"apple_team {t!r} must be a 10-character Team ID")
        bad(not reserved_apple_team(t), "apple_team is ArtCraft's Developer ID")
    w = d.get("website")
    if w is not None:
        bad(valid_https(w), f"website {w!r} must be an https:// link")
    bad(parse_color(d["color"]) is not None, f"color {d['color']!r} must be #rrggbb")
    i = d.get("icon")
    if i is not None:
        bad(valid_file_name(i) and ascii_lower(i).endswith(".png"), f"icon {i!r} must be the file name of a .png release asset")
    bad(
        len(d["former_ids"]) <= MAX_LIST and len(d["former_names"]) <= MAX_LIST and len(d["former_bundles"]) <= MAX_LIST,
        f"at most {MAX_LIST} former ids, names and bundles each",
    )
    for f in d["former_ids"]:
        bad(valid_id(f) and not reserved_id(f), f"former id {f!r} isn't a valid unofficial id")
    for f in d["former_names"]:
        bad(valid_text(f, 40) and not reserved_id(f), f"former name {f!r} isn't a valid unofficial name")
    for f in d["former_bundles"]:
        bad(valid_bundle(f) and not reserved_bundle(f), f"former bundle {f!r} isn't a valid unofficial bundle id")
    return p


def descriptor_json(d: dict) -> str:
    """`Descriptor::to_json`: pretty JSON in field order, optional fields left out when empty."""
    out = {}
    for k in DESCRIPTOR_ORDER:
        kind = DESCRIPTOR_FIELDS[k]
        v = d.get(k)
        if kind == "opt" and v is None:
            continue
        if kind == "list" and not v:
            continue
        out[k] = v
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"


def stems(d: dict) -> list[str]:
    return [d["id"], *d.get("former_ids", [])]


_NUM = r"(?:0|[1-9][0-9]*)"
_PRE = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_BUILD = r"[0-9A-Za-z-]+"
_SEMVER = re.compile(rf"({_NUM})\.({_NUM})\.({_NUM})(?:-{_PRE}(?:\.{_PRE})*)?(?:\+{_BUILD}(?:\.{_BUILD})*)?")


def valid_semver(v: str) -> bool:
    """`semver::Version::parse` (SemVer 2.0; numbers fit a u64, no leading zeros)."""
    m = _SEMVER.fullmatch(v)
    return bool(m) and all(int(g) < 2**64 for g in m.groups())


@dataclass
class Package:
    name: str
    version: str
    token: str
    suffix: str


def parse_package(name: str, stem_list: list[str]) -> Package | None:
    """`contract::parse_package`."""
    for s in SUFFIXES:
        if name.endswith(s):
            suffix, base = s, name[: -len(s)]
            break
    else:
        return None
    for stem in stem_list:
        if not base.startswith(stem):
            continue
        rest = base[len(stem):]
        if not rest.startswith("-"):
            continue
        parts = rest[1:].rsplit("-", 2)
        if len(parts) != 3:
            continue
        version, os_, arch = parts
        if version[:1] in _ASCII_DIGIT and valid_semver(version):
            return Package(name, version, f"{os_}-{arch}", suffix)
    return None


def _rust_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def parse_sha256sums(text: str) -> list[tuple[str, str]]:
    """`parse_sha256sums`: `<hex>  <name>` or `<hex> *<name>`; malformed lines skipped."""
    out = []
    for line in _rust_lines(text):
        line = rust_trim(line)
        idx = next((i for i, c in enumerate(line) if c in _RUST_WS), None)
        if idx is None:
            continue
        hsh, rest = line[:idx], line[idx + 1:]
        j = 0
        while j < len(rest) and rest[j] in _RUST_WS:
            j += 1
        name = rest[j:].lstrip("*")
        if len(hsh) == 64 and all(c in _HEX for c in hsh) and valid_file_name(name):
            out.append((ascii_lower(hsh), name))
    return out


def check_icon(data: bytes) -> tuple[int, int] | str:
    """`check_icon`: (width, height) or the reason it's refused."""
    if len(data) > MAX_ICON_BYTES:
        return f"the icon is {len(data)} bytes; the limit is {MAX_ICON_BYTES}"
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        return "the icon isn't a PNG"
    if len(data) < 24:
        return "the icon's PNG header is cut short"
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if w == 0 or h == 0 or w > MAX_ICON_EDGE or h > MAX_ICON_EDGE:
        return f"the icon is {w}×{h}; it must be between 1 and {MAX_ICON_EDGE} pixels on each side"
    return w, h


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check_release(d: dict, tag: str, assets: list[str], sums: str | None) -> Report:
    """`contract::check_release`."""
    r = Report()
    st = stems(d)
    for name in (DESCRIPTOR_FILE, SUMS_FILE):
        if name not in assets:
            r.problems.append(f"{name} isn't attached")
    ignored = []
    for name in assets:
        p = parse_package(name, st)
        if p:
            r.packages.append(p)
        elif any(name.endswith(s) for s in SUFFIXES) and any(name.startswith(f"{s}-") for s in st):
            r.problems.append(f"{name} looks like a package but isn't named <id>-<version>-<os>-<arch>[-portable].<ext>")
        elif name in (DESCRIPTOR_FILE, SUMS_FILE) or d.get("icon") == name:
            pass
        else:
            ignored.append(name)
    if not r.packages:
        r.problems.append(f"no package is named {d['id']}-<version>-<os>-<arch>[-portable].<ext>")
    want = tag[1:] if tag.startswith("v") else tag
    for p in r.packages:
        if p.version != want:
            r.problems.append(f"{p.name} is version {p.version}, but the tag says {want}")
        if p.token not in TOKENS:
            r.problems.append(f"{p.name}: {p.token} isn't a known <os>-<arch>")
    if any(p.suffix == ".msi" for p in r.packages) and d.get("msi_publisher") is None:
        r.problems.append("the release ships an .msi, so craft-app.json must name msi_publisher")
    if any(p.suffix == ".dmg" for p in r.packages) and d.get("apple_team") is None:
        r.warnings.append("no apple_team: the launcher won't install the .dmg, only open an app that's already there")
    icon = d.get("icon")
    if icon is not None and icon not in assets:
        r.problems.append(f"the icon {icon} isn't attached")
    if sums is not None:
        listed = [n for _, n in parse_sha256sums(sums)]
        must = [p.name for p in r.packages] + [DESCRIPTOR_FILE] + ([icon] if icon else [])
        for name in must:
            if name not in listed:
                r.problems.append(f"{SUMS_FILE} doesn't list {name}")
        for name in listed:
            if name not in assets:
                r.problems.append(f"{SUMS_FILE} lists {name}, which isn't attached")
    elif SUMS_FILE in assets:
        r.warnings.append(f"{SUMS_FILE} wasn't read, so the checksums weren't compared")
    if ignored:
        r.warnings.append(f"ignored (not packages): {', '.join(ignored)}")
    return r


# ---------------------------------------------------------------------------------------------
# Archive layouts the launcher installs (crates/native: portable.rs and install.rs).
# ---------------------------------------------------------------------------------------------

ZIP_MAX_FILES = 10_000
ZIP_MAX_BYTES = 4 * 1024 * 1024 * 1024
TAR_MAX_ENTRIES = 50_000
TAR_MAX_BYTES = 2 * 1024 * 1024 * 1024


def _zip_parts(name: str) -> list[str] | None:
    """A zip entry's path components, or None when it's unsafe (absolute, `..`, `\\`, `:`)."""
    if "\\" in name or ":" in name or name.startswith("/") or "\x00" in name:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return parts


def check_zip_layout(path: Path, stem_list: list[str]) -> list[str]:
    """Problems that would stop the launcher installing a Windows portable zip: unsafe paths or
    links, duplicate names (case-insensitive), size limits, and `<id>.exe` not at the root or at
    the root of one wrapper folder."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, OSError) as e:
        return [f"{path.name}: not a readable zip ({e})"]
    if len(infos) > ZIP_MAX_FILES:
        return [f"{path.name}: more than {ZIP_MAX_FILES} entries"]
    files, entries, seen, total = [], [], set(), 0
    for i in infos:
        # orig_filename: on Windows zipfile turns `\` into `/` in `filename`.
        if "\\" in i.orig_filename:
            return [f"{path.name}: entry {i.orig_filename!r} uses \\ as a folder separator, which the launcher refuses"]
        parts = _zip_parts(i.orig_filename)
        mode = i.external_attr >> 16
        if parts is None or (i.create_system == 3 and stat.S_ISLNK(mode)):
            return [f"{path.name}: unsafe path or link {i.orig_filename!r}"]
        key = "/".join(parts).lower()
        if key in seen:
            return [f"{path.name}: duplicate path {i.filename!r}"]
        seen.add(key)
        entries.append(parts)
        if i.is_dir():
            continue
        total += i.file_size
        if total > ZIP_MAX_BYTES:
            return [f"{path.name}: unpacks to more than {ZIP_MAX_BYTES} bytes"]
        files.append(parts)
    exe_names = {f"{s}.exe".lower() for s in stem_list}
    root_exe = lambda fs: any(len(f) == 1 and f[0].lower() in exe_names for f in fs)  # noqa: E731
    if root_exe(files):
        return []
    if not files:
        return [f"{path.name}: contains no files"]
    prefix = files[0][0]
    if not all(e[0] == prefix for e in entries) or not all(len(f) > 1 and f[0] == prefix for f in files):
        return [f"{path.name}: has no {stem_list[0]}.exe at its root and isn't one wrapper folder"]
    if not root_exe([f[1:] for f in files]):
        return [f"{path.name}: {stem_list[0]}.exe isn't at the root of its wrapper folder {prefix}/"]
    return []


def _tar_parts(name: str) -> list[str] | None:
    if name.startswith("/") or "\x00" in name:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return parts


def check_tar_layout(path: Path, app_id: str) -> list[str]:
    """Problems that would stop the launcher installing a Linux/FreeBSD tarball: after an
    optional single top folder, a regular file at `bin/<id>` or `<id>`, reached without links."""
    try:
        with tarfile.open(path, "r:gz") as t:
            members = t.getmembers()
    except (tarfile.TarError, OSError, EOFError) as e:
        return [f"{path.name}: not a readable .tar.gz ({e})"]
    if len(members) > TAR_MAX_ENTRIES:
        return [f"{path.name}: more than {TAR_MAX_ENTRIES} entries"]
    total, by_path = 0, {}
    for m in members:
        parts = _tar_parts(m.name)
        if parts is None:
            return [f"{path.name}: unsafe path {m.name!r}"]
        if not parts:
            continue
        total += m.size
        if total > TAR_MAX_BYTES:
            return [f"{path.name}: unpacks to more than {TAR_MAX_BYTES} bytes"]
        by_path[tuple(parts)] = m
    tops = {p[0] for p in by_path}
    top: tuple[str, ...] = ()
    if len(tops) == 1:
        only = next(iter(tops))
        m = by_path.get((only,))
        if m is None or m.isdir():
            top = (only,)
    for cand in (top + ("bin", app_id), top + (app_id,)):
        m = by_path.get(cand)
        if m is None or not m.isfile():
            continue
        if any((by_path.get(cand[:k]) is not None and not by_path[cand[:k]].isdir()) for k in range(1, len(cand))):
            return [f"{path.name}: the path to {'/'.join(cand)} goes through a link"]
        return []
    where = "/".join(top) + "/" if top else ""
    return [f"{path.name}: no regular file at {where}bin/{app_id} or {where}{app_id}"]


def repack_zip(src: Path, dst: Path, stem_list: list[str]) -> None:
    """Rewrites a portable zip so `<id>.exe` sits at the root: the folder holding the executable
    becomes the archive root, and every path uses `/`. Every file must live in that folder
    (nothing is dropped). File bytes are copied unchanged."""
    exe_names = {f"{s}.exe".lower() for s in stem_list}
    with zipfile.ZipFile(src) as z:
        # Some Windows tools write `\` as the separator, which the launcher refuses; when
        # rewriting, it is read as a folder separator and written back as `/`.
        norm = {i.filename: i.orig_filename.replace("\\", "/") for i in z.infolist()}
        infos = [i for i in z.infolist() if not norm[i.filename].endswith("/")]
        parts = {}
        for i in infos:
            p = _zip_parts(norm[i.filename])
            if p is None or (i.create_system == 3 and stat.S_ISLNK(i.external_attr >> 16)):
                raise BridgeError(f"{src.name}: unsafe path or link {i.orig_filename!r}")
            if "/".join(p).lower() in {"/".join(x).lower() for x in parts.values()}:
                raise BridgeError(f"{src.name}: duplicate path {i.orig_filename!r}")
            parts[i.filename] = p
        exes = sorted((p for p in parts.values() if p[-1].lower() in exe_names), key=len)
        if not exes:
            raise BridgeError(f"{src.name}: no {stem_list[0]}.exe anywhere in the zip")
        root = exes[0][:-1]
        n = len(root)
        outside = [i.filename for i in infos if parts[i.filename][:n] != root]
        if outside:
            raise BridgeError(f"{src.name}: files outside {'/'.join(root)}/ would be lost by repacking: {outside[:5]}")
        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as out:
            for i in infos:
                info = zipfile.ZipInfo("/".join(parts[i.filename][n:]), date_time=i.date_time)
                info.external_attr = i.external_attr
                info.compress_type = zipfile.ZIP_DEFLATED
                with z.open(i) as r, out.open(info, "w", force_zip64=True) as w:
                    shutil.copyfileobj(r, w, 1024 * 1024)


def repack_tar(src: Path, dst: Path, app_id: str, top_name: str) -> None:
    """Rewrites a tarball as `<top_name>/bin/<id>` plus everything else beside `bin/`. The
    executable's folder (or the folder above a `bin/` holding it) becomes the tree root; every
    entry must live under it. `<id>` and `<id>-cli` found at the root move into `bin/`. File bytes
    and modes are kept; links must stay inside the tree; hard links are refused."""
    with tarfile.open(src, "r:gz") as t:
        members = []
        for m in t.getmembers():
            p = _tar_parts(m.name)
            if p is None:
                raise BridgeError(f"{src.name}: unsafe path {m.name!r}")
            if p:
                members.append((p, m))
        bins = sorted((p for p, m in members if m.isfile() and p[-1] == app_id), key=len)
        if not bins:
            raise BridgeError(f"{src.name}: no file named {app_id} in the tarball")
        root = bins[0][:-1]
        if root and root[-1] == "bin":
            root = root[:-1]
        n = len(root)
        outside = ["/".join(p) for p, m in members if p[:n] != root and not (m.isdir() and len(p) < n)]
        if outside:
            raise BridgeError(f"{src.name}: entries outside {'/'.join(root) or '.'}/ would be lost by repacking: {outside[:5]}")
        with tarfile.open(dst, "w:gz", format=tarfile.PAX_FORMAT) as out:
            top = tarfile.TarInfo(top_name)
            top.type, top.mode = tarfile.DIRTYPE, 0o755
            out.addfile(top)
            have_bin = False
            for p, m in members:
                if len(p) <= n:
                    continue
                rel = list(p[n:])
                if len(rel) == 1 and m.isfile() and rel[0] in (app_id, f"{app_id}-cli"):
                    rel = ["bin", rel[0]]
                if rel[0] == "bin" and not have_bin:
                    d = tarfile.TarInfo(f"{top_name}/bin")
                    d.type, d.mode = tarfile.DIRTYPE, 0o755
                    out.addfile(d)
                    have_bin = True
                if rel == ["bin"]:
                    continue
                info = tarfile.TarInfo(f"{top_name}/" + "/".join(rel))
                info.mode, info.mtime, info.uid, info.gid = m.mode, m.mtime, 0, 0
                if m.isdir():
                    info.type = tarfile.DIRTYPE
                    out.addfile(info)
                elif m.issym():
                    target = PurePosixPath(m.linkname)
                    resolved = [*rel[:-1]]
                    for part in target.parts:
                        if part == "..":
                            if not resolved:
                                raise BridgeError(f"{src.name}: link {m.name} points outside the tree")
                            resolved.pop()
                        elif part != ".":
                            resolved.append(part)
                    if target.is_absolute():
                        raise BridgeError(f"{src.name}: link {m.name} is absolute")
                    info.type, info.linkname = tarfile.SYMTYPE, m.linkname
                    out.addfile(info)
                elif m.isfile():
                    info.size = m.size
                    with t.extractfile(m) as r:
                        out.addfile(info, r)
                else:
                    raise BridgeError(f"{src.name}: {m.name} is a hard link or special file")


# ---------------------------------------------------------------------------------------------
# Sums and the local release check (port of craft-release-check `dir`).
# ---------------------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sums_text(files: dict[str, str]) -> str:
    """`sha256sum` text for {name: hex}, sorted by name, SHA256SUMS.txt and dot files left out."""
    return "".join(f"{h}  {n}\n" for n, h in sorted(files.items()) if n != SUMS_FILE and not n.startswith("."))


def write_sums(dist: Path) -> str:
    names = sorted(p.name for p in dist.iterdir() if p.is_file())
    text = sums_text({n: sha256_file(dist / n) for n in names if n != SUMS_FILE and not n.startswith(".")})
    (dist / SUMS_FILE).write_text(text, encoding="utf-8", newline="\n")
    return text


def check_dir(dist: Path, repo: str, tag: str, layouts: bool = True) -> Report:
    """craft-release-check `dir`, plus the launcher's archive layout rules."""
    out = Report()
    files = sorted(p.name for p in dist.iterdir() if p.is_file())
    d = None
    if DESCRIPTOR_FILE in files:
        raw = (dist / DESCRIPTOR_FILE).read_bytes()
        d, problems = parse_descriptor(raw, repo)
        out.problems += problems
    else:
        out.problems.append(f"{DESCRIPTOR_FILE} isn't in {dist}")
    sums = None
    if SUMS_FILE in files:
        raw = (dist / SUMS_FILE).read_bytes()
        if len(raw) > MAX_SUMS_BYTES:
            out.problems.append(f"{SUMS_FILE} is larger than {MAX_SUMS_BYTES} bytes")
        else:
            try:
                sums = raw.decode("utf-8")
            except UnicodeDecodeError:
                out.problems.append(f"{SUMS_FILE} isn't UTF-8 text")
    if sums is not None:
        parsed = parse_sha256sums(sums)
        for want, name in parsed:
            if name in files:
                got = sha256_file(dist / name)
                if got != want:
                    out.problems.append(f"{name}: its SHA-256 is {got}, but {SUMS_FILE} says {want}")
        malformed = sum(1 for line in _rust_lines(sums) if rust_trim(line)) - len(parsed)
        if malformed > 0:
            out.warnings.append(f"{SUMS_FILE} has {malformed} line(s) that aren't `<sha256>  <file name>`; the launcher ignores them")
    if d is None:
        out.problems.append(f"the release contract wasn't checked, because {DESCRIPTOR_FILE} couldn't be read")
        return out
    icon = d.get("icon")
    if icon and icon in files:
        raw = (dist / icon).read_bytes()
        res = check_icon(raw)
        if isinstance(res, str):
            out.problems.append(f"{icon}: {res}")
    for name in files:
        if not valid_file_name(name) and not name.startswith("."):
            out.problems.append(f"{name!r} can't be a release asset name")
    r = check_release(d, tag, [n for n in files if not n.startswith(".")], sums)
    out.problems += r.problems
    out.warnings += r.warnings
    out.packages = r.packages
    if layouts:
        for p in r.packages:
            if p.suffix == "-portable.zip":
                out.problems += check_zip_layout(dist / p.name, stems(d))
            elif p.suffix == ".tar.gz" and (p.token.startswith("linux-") or p.token.startswith("freebsd-")):
                out.problems += check_tar_layout(dist / p.name, d["id"])
    return out


# ---------------------------------------------------------------------------------------------
# Config.
# ---------------------------------------------------------------------------------------------


class BridgeError(Exception):
    """The release can't be built (exit 1)."""


class ConfigError(Exception):
    """bridge.toml is wrong (exit 2)."""


DESCRIPTOR_CONFIG_KEYS = {"id", "name", "tagline", "publisher", "bundle_id", "msi_publisher", "apple_team", "website", "color", "former_ids", "former_names", "former_bundles"}


@dataclass
class Rule:
    match: re.Pattern
    to: str
    repack: str = "auto"  # auto | always | never
    map: dict = field(default_factory=dict)


@dataclass
class Config:
    upstream: str
    repo: str
    descriptor: dict
    rules: list[Rule]
    skip: list[re.Pattern]
    upstream_sums: list[str]
    licenses: list[str] | None  # None: auto-detect at the tag
    icon: dict
    max_asset_bytes: int = 1024 * 1024 * 1024
    strict: bool = False
    notes_chars: int = 4000


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"can't read {path}: {e}") from e
    unknown = set(raw) - {"bridge", "descriptor", "icon", "rename"}
    if unknown:
        raise ConfigError(f"unknown tables in {path}: {sorted(unknown)}")
    b = raw.get("bridge", {})
    unknown = set(b) - {"upstream", "repo", "upstream_sums", "licenses", "max_asset_mb", "strict", "notes_chars", "skip"}
    if unknown:
        raise ConfigError(f"unknown keys in [bridge]: {sorted(unknown)}")
    for k in ("upstream", "repo"):
        if not isinstance(b.get(k), str) or not valid_repo(b[k]):
            raise ConfigError(f"[bridge] {k} must be a GitHub owner/name")
    desc = raw.get("descriptor", {})
    unknown = set(desc) - DESCRIPTOR_CONFIG_KEYS
    if unknown:
        raise ConfigError(f"unknown keys in [descriptor]: {sorted(unknown)} (repo, upstream, schema and icon are filled in)")
    rules = []
    for i, r in enumerate(raw.get("rename", [])):
        if set(r) - {"match", "to", "repack", "map"} or "match" not in r or "to" not in r:
            raise ConfigError(f"[[rename]] #{i + 1} needs `match` and `to` (and optionally `repack`, `map`)")
        if r.get("repack", "auto") not in ("auto", "always", "never"):
            raise ConfigError(f"[[rename]] #{i + 1}: repack must be auto, always or never")
        try:
            rules.append(Rule(re.compile(r["match"]), r["to"], r.get("repack", "auto"), r.get("map", {})))
        except re.error as e:
            raise ConfigError(f"[[rename]] #{i + 1}: bad regex: {e}") from e
    skip = b.get("skip", [])
    try:
        skip_res = [re.compile(s) for s in skip]
    except re.error as e:
        raise ConfigError(f"skip: bad regex: {e}") from e
    icon = raw.get("icon", {})
    if set(icon) - {"url", "archive", "member", "name"}:
        raise ConfigError("[icon] takes url, or archive + member, and optionally name")
    if icon and not (("url" in icon) ^ ("archive" in icon and "member" in icon)):
        raise ConfigError("[icon] needs either url or archive + member")
    lic = b.get("licenses", "auto")
    return Config(
        upstream=b["upstream"],
        repo=b["repo"],
        descriptor=dict(desc),
        rules=rules,
        skip=skip_res,
        upstream_sums=list(b.get("upstream_sums", [])),
        licenses=None if lic == "auto" else list(lic),
        icon=icon,
        max_asset_bytes=int(b.get("max_asset_mb", 1024)) * 1024 * 1024,
        strict=bool(b.get("strict", False)),
        notes_chars=int(b.get("notes_chars", 4000)),
    )


def build_descriptor(cfg: Config) -> dict:
    d = {k: None for k, kind in DESCRIPTOR_FIELDS.items() if kind == "opt"}
    d.update({k: [] for k, kind in DESCRIPTOR_FIELDS.items() if kind == "list"})
    d.update(cfg.descriptor)
    d["schema"] = SCHEMA
    d["repo"] = cfg.repo
    d["upstream"] = cfg.upstream
    if cfg.icon:
        d["icon"] = cfg.icon.get("name", f"{d.get('id', 'app')}.png")
    for k, kind in DESCRIPTOR_FIELDS.items():
        if kind == "str" and k not in d:
            raise ConfigError(f"[descriptor] needs {k}")
    return d


def version_of(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def plan_asset(cfg: Config, name: str, app_id: str, version: str) -> tuple[str, str | None, Rule | None, str]:
    """('skip' | 'rename' | 'unmatched', new name, rule, why) for one upstream asset name."""
    for s in cfg.skip:
        if s.search(name):
            return "skip", None, None, f"skip pattern {s.pattern!r}"
    for rule in cfg.rules:
        m = rule.match.fullmatch(name)
        if not m:
            continue
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        if "version" in groups and groups["version"] != version:
            return "skip", None, rule, f"its version {groups['version']} isn't the tag's {version}"
        for k, table in rule.map.items():
            if k in groups:
                groups[k] = table.get(groups[k], groups[k])
        groups.update(id=app_id, version=version)
        try:
            new = rule.to.format(**groups)
        except (KeyError, IndexError, ValueError) as e:
            raise ConfigError(f"rename template {rule.to!r}: {e}") from e
        return "rename", new, rule, f"rule {rule.match.pattern!r}"
    return "unmatched", None, None, "no rule matches"


# ---------------------------------------------------------------------------------------------
# GitHub.
# ---------------------------------------------------------------------------------------------

DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com", "raw.githubusercontent.com", "codeload.github.com")


class NetError(Exception):
    """A network or API failure (exit 2)."""


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or (u.hostname or "") not in DOWNLOAD_HOSTS + ("api.github.com",):
            raise NetError(f"refusing a redirect to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnly)


def _open(url: str, token: str | None, accept: str):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or (u.hostname or "") not in DOWNLOAD_HOSTS + ("api.github.com",):
        raise NetError(f"refusing to fetch {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    if token and u.hostname == "api.github.com":
        # Unredirected: never forwarded to the asset CDN.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
        req.add_unredirected_header("X-GitHub-Api-Version", "2022-11-28")
    last = None
    for attempt in range(4):
        try:
            return _opener.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
                raise NetError(f"GitHub rate limit reached fetching {url} (set GITHUB_TOKEN)") from e
            if e.code < 500 and e.code not in (408, 429):
                raise NetError(f"{url}: HTTP {e.code}") from e
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(2 ** attempt)
    raise NetError(f"{url}: {last}")


def fetch_bytes(url: str, token: str | None, max_bytes: int, accept: str = "application/vnd.github+json") -> bytes | None:
    r = _open(url, token, accept)
    if r is None:
        return None
    with r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise NetError(f"{url} is larger than {max_bytes} bytes")
    return data


def fetch_json(path: str, token: str | None):
    data = fetch_bytes(f"{API}{path}", token, 16 * 1024 * 1024)
    return None if data is None else json.loads(data)


def download(url: str, dest: Path, max_bytes: int, token: str | None = None) -> tuple[int, str]:
    """Streams `url` to `dest` (at most `max_bytes`); returns (size, sha256)."""
    r = _open(url, token, "application/octet-stream")
    if r is None:
        raise NetError(f"{url}: 404")
    h, n = hashlib.sha256(), 0
    with r, open(dest, "wb") as f:
        while chunk := r.read(1024 * 1024):
            n += len(chunk)
            if n > max_bytes:
                raise BridgeError(f"{url} is larger than the {max_bytes}-byte cap")
            h.update(chunk)
            f.write(chunk)
    return n, h.hexdigest()


def gh(args: list[str], token: str | None, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if token and not env.get("GH_TOKEN"):
        env["GH_TOKEN"] = token
    cp = subprocess.run(["gh", *args], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and cp.returncode != 0:
        raise NetError(f"gh {' '.join(args[:3])} failed: {cp.stderr.strip() or cp.stdout.strip()}")
    return cp


def bridge_release_state(repo: str, tag: str, token: str | None) -> str:
    """'published', 'draft' or 'none' for `tag` in the bridge repo."""
    rel = fetch_json(f"/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}", token)
    if rel is not None:
        return "draft" if rel.get("draft") else "published"
    if token:
        rels = fetch_json(f"/repos/{repo}/releases?per_page=100", token) or []
        if any(r.get("tag_name") == tag and r.get("draft") for r in rels):
            return "draft"
    return "none"


# ---------------------------------------------------------------------------------------------
# Building one release.
# ---------------------------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg, flush=True)


def fence(text: str) -> str:
    """A fenced block no backtick run inside `text` can close (keeps upstream notes inert: no
    @mentions or cross-references are rendered from inside a code block)."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    f = "`" * max(3, longest + 1)
    return f"{f}text\n{text}\n{f}"


@dataclass
class Built:
    tag: str
    dist: Path
    renamed: list[tuple[str, str, str]]  # (upstream name, our name, what was done)
    skipped: list[tuple[str, str]]
    report: Report
    notes: str


def upstream_adopted(cfg: Config, release: dict, token: str | None) -> bool:
    """Whether the upstream release itself carries a craft-app.json valid for the upstream repo."""
    asset = next((a for a in release.get("assets", []) if a.get("name") == DESCRIPTOR_FILE), None)
    if asset is None:
        return False
    data = fetch_bytes(asset["browser_download_url"], None, MAX_DESCRIPTOR_BYTES + 1, "application/octet-stream")
    if data is None:
        return False
    d, problems = parse_descriptor(data, cfg.upstream)
    if d is None:
        log(f"warning: {cfg.upstream} attaches a {DESCRIPTOR_FILE} that isn't valid ({'; '.join(problems)}); mirroring anyway")
        return False
    return True


def build_release(cfg: Config, release: dict, out_root: Path, token: str | None) -> Built:
    tag = release["tag_name"]
    version = version_of(tag)
    if not (version[:1] in _ASCII_DIGIT and valid_semver(version)):
        raise BridgeError(f"upstream tag {tag!r} isn't v<semver>; nothing can be named after it")
    d = build_descriptor(cfg)
    problems = check_descriptor(d, cfg.repo)
    if problems:
        raise ConfigError("the [descriptor] table isn't valid: " + "; ".join(problems))
    app_id = d["id"]
    st = stems(d)
    dist = out_root / tag
    work = out_root / ".work" / tag
    for p in (dist, work):
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True)
    assets = release.get("assets", [])
    by_name = {a["name"]: a for a in assets}

    # Upstream's own checksums, when it publishes them.
    upstream_sums: dict[str, str] = {}
    for sname in cfg.upstream_sums:
        a = by_name.get(sname)
        if a is None:
            log(f"note: upstream release has no {sname}; downloads are checked against GitHub's digests only")
            continue
        data = fetch_bytes(a["browser_download_url"], None, MAX_SUMS_BYTES, "application/octet-stream")
        if data is None:
            raise BridgeError(f"couldn't download upstream {sname}")
        for h, n in parse_sha256sums(data.decode("utf-8", "replace")):
            upstream_sums[n] = h
        log(f"upstream {sname}: {len(upstream_sums)} checksum(s)")

    renamed, skipped, targets = [], [], {}
    for a in assets:
        name = a["name"]
        if name in cfg.upstream_sums:
            skipped.append((name, "upstream checksum file (used to verify downloads)"))
            continue
        kind, new, rule, why = plan_asset(cfg, name, app_id, version)
        if kind != "rename":
            if kind == "unmatched" and cfg.strict:
                raise BridgeError(f"upstream asset {name} matches no rename or skip rule (strict = true)")
            skipped.append((name, why))
            continue
        if not valid_file_name(new):
            raise ConfigError(f"rule {rule.match.pattern!r} makes {new!r}, which can't be an asset name")
        if new in targets:
            raise BridgeError(f"{name} and {targets[new]} would both become {new}")
        targets[new] = name
        if a.get("size", 0) > cfg.max_asset_bytes:
            raise BridgeError(f"{name} is {a['size']} bytes, over the {cfg.max_asset_bytes}-byte cap")
        url = a["browser_download_url"]
        if not url.startswith(f"https://github.com/{cfg.upstream}/releases/download/"):
            raise BridgeError(f"{name}'s download URL isn't on {cfg.upstream}'s releases: {url}")
        raw = work / name
        log(f"download {name} ({a.get('size', 0)} bytes)")
        size, digest = download(url, raw, cfg.max_asset_bytes)
        if a.get("size") is not None and size != a["size"]:
            raise BridgeError(f"{name}: downloaded {size} bytes, GitHub says {a['size']}")
        gh_digest = a.get("digest") or ""
        if gh_digest.startswith("sha256:") and gh_digest[7:].lower() != digest:
            raise BridgeError(f"{name}: SHA-256 {digest} doesn't match GitHub's digest {gh_digest}")
        if upstream_sums:
            want = upstream_sums.get(name)
            if want is None:
                log(f"warning: upstream's checksum file doesn't list {name}")
            elif want != digest:
                raise BridgeError(f"{name}: SHA-256 {digest} doesn't match upstream's checksum file ({want}); aborting")
            else:
                log(f"  verified against upstream's checksum file")
        target = dist / new
        what = "renamed" if new != name else "name kept"
        pkg = parse_package(new, st)
        layout_problems = []
        if pkg and pkg.suffix == "-portable.zip":
            layout_problems = check_zip_layout(raw, st)
        elif pkg and pkg.suffix == ".tar.gz" and pkg.token.split("-")[0] in ("linux", "freebsd"):
            layout_problems = check_tar_layout(raw, app_id)
        must_repack = (rule.repack == "always") or (rule.repack == "auto" and layout_problems)
        if pkg and must_repack and pkg.suffix in ("-portable.zip", ".tar.gz"):
            log(f"  repack {name}: {'; '.join(layout_problems) or 'repack = always'}")
            if pkg.suffix == "-portable.zip":
                repack_zip(raw, target, st)
            else:
                repack_tar(raw, target, app_id, new[: -len(".tar.gz")])
            what += "; repacked (archive layout only; the files inside are unchanged)"
            raw.unlink()
        else:
            if layout_problems:
                raise BridgeError("; ".join(layout_problems) + " (repack = never)")
            os.replace(raw, target)
        renamed.append((name, new, what))

    # Icon.
    if cfg.icon:
        icon_name = d["icon"]
        fmt = {"upstream": cfg.upstream, "tag": tag, "version": version, "id": app_id}
        if "url" in cfg.icon:
            url = cfg.icon["url"].format(**fmt)
            data = fetch_bytes(url, None, MAX_ICON_BYTES + 1, "application/octet-stream")
            if data is None:
                raise BridgeError(f"icon {url}: not found")
        else:
            arch_re = re.compile(cfg.icon["archive"].format(**fmt))
            member_re = re.compile(cfg.icon["member"].format(**fmt))
            src = next((dist / new for (old, new, _) in renamed if arch_re.fullmatch(old)), None)
            if src is None:
                raise BridgeError(f"icon: no mirrored asset matches {arch_re.pattern!r}")
            data = _member_bytes(src, member_re, MAX_ICON_BYTES + 1)
        res = check_icon(data)
        if isinstance(res, str):
            raise BridgeError(f"icon: {res} (shrink it with tools/icon.py)")
        (dist / icon_name).write_bytes(data)

    # Licences and notices from upstream at this tag.
    lic_names = cfg.licenses
    if lic_names is None:
        listing = fetch_json(f"/repos/{cfg.upstream}/contents?ref={urllib.parse.quote(tag, safe='')}", token) or []
        lic_names = sorted(
            e["name"] for e in listing if e.get("type") == "file" and re.fullmatch(r"(?i)(LICEN[CS]E|COPYING|NOTICE)([-._][A-Za-z0-9.-]+)?", e["name"])
        )
    if not lic_names:
        raise BridgeError(f"{cfg.upstream} has no LICENSE file at {tag}; refusing to redistribute")
    for ln in lic_names:
        if not valid_file_name(ln) or (dist / ln).exists():
            raise BridgeError(f"licence file name {ln!r} can't be attached")
        url = f"https://raw.githubusercontent.com/{cfg.upstream}/{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(ln)}"
        data = fetch_bytes(url, None, 1024 * 1024, "application/octet-stream")
        if data is None:
            raise BridgeError(f"couldn't fetch {ln} from {cfg.upstream} at {tag}")
        (dist / ln).write_bytes(data)

    (dist / DESCRIPTOR_FILE).write_text(descriptor_json(d), encoding="utf-8", newline="\n")
    write_sums(dist)
    report = check_dir(dist, cfg.repo, tag)
    notes = release_notes(cfg, d, release, renamed, skipped, dist)
    shutil.rmtree(work, ignore_errors=True)
    return Built(tag, dist, renamed, skipped, report, notes)


def _member_bytes(archive: Path, member_re: re.Pattern, cap: int) -> bytes:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for i in z.infolist():
                if member_re.fullmatch(i.filename) and not i.is_dir() and i.file_size <= cap:
                    return z.read(i)
    else:
        with tarfile.open(archive, "r:gz") as t:
            for m in t.getmembers():
                if member_re.fullmatch(m.name) and m.isfile() and m.size <= cap:
                    with t.extractfile(m) as f:
                        return f.read(cap)
    raise BridgeError(f"icon: {archive.name} has no member matching {member_re.pattern!r}")


def release_notes(cfg: Config, d: dict, release: dict, renamed, skipped, dist: Path) -> str:
    sums = dict((n, h) for h, n in parse_sha256sums((dist / SUMS_FILE).read_text(encoding="utf-8")))
    lines = [
        f"**Unofficial mirror** of [{cfg.upstream} {release['tag_name']}]({release.get('html_url', f'https://github.com/{cfg.upstream}/releases')}) "
        f"for Craft Unofficial Launcher, published by [{cfg.repo}](https://github.com/{cfg.repo}). "
        f"It is not affiliated with or endorsed by {d['publisher']} or ArtCraft.",
        "",
        "The packages are upstream's own builds, downloaded from the release above and checked against its checksums. "
        "They are renamed to the launcher's naming contract; an archive is repacked only if its layout needed it (marked below), "
        "and no program file is changed.",
        "",
        "| File | Upstream asset | SHA-256 |",
        "|---|---|---|",
    ]
    for old, new, what in renamed:
        mark = " (repacked)" if "repacked" in what else ""
        lines.append(f"| `{new}`{mark} | `{old}` | `{sums.get(new, '')}` |")
    if skipped:
        lines += ["", "Not mirrored: " + ", ".join(f"`{n}`" for n, _ in skipped) + "."]
    body = (release.get("body") or "").strip()
    if body:
        if len(body) > cfg.notes_chars:
            body = body[: cfg.notes_chars].rstrip() + "\n[… truncated; see the upstream release]"
        lines += ["", "<details><summary>Upstream release notes</summary>", "", fence(body), "", "</details>"]
    return "\n".join(lines) + "\n"


def publish(cfg: Config, d_name: str, built: Built, token: str | None, latest: bool, replace: bool) -> None:
    tag = built.tag
    state = bridge_release_state(cfg.repo, tag, token)
    if state != "none":
        if state == "published" and not replace:
            raise BridgeError(f"{cfg.repo} already has a release {tag}")
        log(f"deleting the existing {state} release {tag} before publishing again")
        gh(["release", "delete", tag, "--repo", cfg.repo, "--yes", "--cleanup-tag"], token)
    notes_file = built.dist.parent / f".notes-{tag}.md"
    notes_file.write_text(built.notes, encoding="utf-8")
    files = sorted(str(p) for p in built.dist.iterdir() if p.is_file() and not p.name.startswith("."))
    title = f"{d_name} {tag} (unofficial mirror)"
    # Draft first, then publish: a run that dies mid-upload leaves a draft, never a half release.
    gh(["release", "create", tag, *files, "--repo", cfg.repo, "--title", title, "--notes-file", str(notes_file), "--draft"], token)
    gh(["release", "edit", tag, "--repo", cfg.repo, "--draft=false", f"--latest={'true' if latest else 'false'}"], token)
    rel = fetch_json(f"/repos/{cfg.repo}/releases/tags/{urllib.parse.quote(tag, safe='')}", token)
    if rel is None:
        raise NetError(f"published {tag}, but GitHub doesn't show it yet")
    got = {a["name"]: a.get("size") for a in rel.get("assets", [])}
    want = {Path(f).name: Path(f).stat().st_size for f in files}
    if got != want:
        raise BridgeError(f"the published assets don't match what was built: {sorted(set(got) ^ set(want))}")
    log(f"published {rel.get('html_url')}")


def print_report(title: str, r: Report) -> None:
    log(title)
    for p in r.packages:
        log(f"  {p.name}  ({p.token})")
    for w in r.warnings:
        log(f"warning: {w}")
    for p in r.problems:
        log(f"problem: {p}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for w in r.warnings:
            log(f"::warning title=craft-bridge::{w}")
        for p in r.problems:
            log(f"::error title=craft-bridge::{p}")
    log("OK" if r.ok else f"FAILED: {len(r.problems)} problem(s)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bridge.py", description="Mirror upstream releases under the craft-app.json contract.")
    ap.add_argument("--config", default="bridge.toml", type=Path)
    ap.add_argument("--out", default="dist", type=Path, help="build folder (default ./dist)")
    ap.add_argument("--dry-run", action="store_true", help="build and check into --out; publish nothing")
    ap.add_argument("--force", action="store_true", help="build (and replace) even if the bridge already has the tag")
    ap.add_argument("--tag", help="mirror this upstream tag instead of the latest release")
    ap.add_argument("--backfill", type=int, default=0, metavar="N", help="also mirror up to N older releases the bridge lacks")
    ap.add_argument("--check", type=Path, metavar="DIR", help="only validate DIR as release --tag of this bridge")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
    try:
        cfg = load_config(args.config)
        if gr := os.environ.get("GITHUB_REPOSITORY"):
            if ascii_lower(gr) != ascii_lower(cfg.repo):
                raise ConfigError(f"bridge.toml says repo = {cfg.repo}, but this runs in {gr}")
        if args.check:
            if not args.tag:
                raise ConfigError("--check needs --tag")
            r = check_dir(args.check, cfg.repo, args.tag)
            print_report(f"{args.check} as {cfg.repo} {args.tag}", r)
            return 0 if r.ok else 1
        if args.tag:
            rel = fetch_json(f"/repos/{cfg.upstream}/releases/tags/{urllib.parse.quote(args.tag, safe='')}", token)
            if rel is None or rel.get("draft"):
                raise NetError(f"{cfg.upstream} has no published release {args.tag}")
            releases = [rel]
        else:
            latest = fetch_json(f"/repos/{cfg.upstream}/releases/latest", token)
            if latest is None:
                log(f"{cfg.upstream} has no published release yet; nothing to do")
                return 0
            releases = [latest]
            if args.backfill > 0:
                all_rel = fetch_json(f"/repos/{cfg.upstream}/releases?per_page=100", token) or []
                older = [r for r in all_rel if not r.get("draft") and not r.get("prerelease") and r["tag_name"] != latest["tag_name"]]
                releases = list(reversed(older[: args.backfill])) + releases
        newest = releases[-1]["tag_name"]
        name = cfg.descriptor.get("name", "")
        failed = False
        for rel in releases:
            tag = rel["tag_name"]
            if upstream_adopted(cfg, rel, token):
                log(f"NOTICE: {cfg.upstream} now follows the procedure ({tag} attaches a valid {DESCRIPTOR_FILE}); "
                    f"point the index at https://github.com/{cfg.upstream} and retire this bridge. Nothing published.")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    log(f"::notice title=craft-bridge::{cfg.upstream} now follows the procedure; point the index at it and retire this bridge")
                return 0
            state = bridge_release_state(cfg.repo, tag, token)
            if state == "published" and not args.force and not args.dry_run:
                log(f"{cfg.repo} already mirrors {tag}; nothing to do")
                continue
            log(f"== mirroring {cfg.upstream} {tag} -> {cfg.repo} (bridge release: {state})")
            try:
                built = build_release(cfg, rel, args.out, token)
            except BridgeError as e:
                log(f"problem: {e}")
                if os.environ.get("GITHUB_ACTIONS") == "true":
                    log(f"::error title=craft-bridge::{e}")
                failed = True
                continue
            for old, new, what in built.renamed:
                log(f"  {old} -> {new} ({what})")
            for old, why in built.skipped:
                log(f"  skipped {old}: {why}")
            print_report(f"{built.dist} as {cfg.repo} {tag}", built.report)
            if not built.report.ok:
                failed = True
                continue
            if args.dry_run:
                log(f"dry run: not publishing; the release is in {built.dist}")
                continue
            publish(cfg, name, built, token, latest=(tag == newest), replace=args.force)
        return 1 if failed else 0
    except ConfigError as e:
        log(f"config error: {e}")
        return 2
    except NetError as e:
        log(f"network error: {e}")
        return 2
    except BridgeError as e:
        log(f"problem: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
