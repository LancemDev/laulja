from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from ..auth.paths import config_directory, ensure_config_directory
from ..models import ArtistRef, Track

"""
Keeps unwanted artists — mainly AI-generated "artists" — out of radio queues and search
results. Two sources are merged:

- blocklist.json: the user's own blocks (added with "b" in the TUI) plus an "allow" list that
  overrides everything, for when a shared list wrongly flags someone.
- shared_blocklist.txt: community lists of AI channels, downloaded on demand with
  `laulja --update-blocklist` and never bundled with the app.

Matching by YouTube channel ID is exact. Community lists that only publish @handles are
matched against artist names with spaces/punctuation stripped ("@SuperCatLeague" ↔ "Super Cat
League") — useful, but a heuristic, so very short handles are ignored to avoid false positives.
"""

LOCAL_FILE_NAME = "blocklist.json"
SHARED_FILE_NAME = "shared_blocklist.txt"

DEFAULT_SHARED_SOURCES: Tuple[str, ...] = (
    # AiSList (community-reviewed AI channels, mostly @handles) — CC BY-NC 4.0, aisloplist.com
    "https://raw.githubusercontent.com/Override92/AiSList/main/AiSList/aislist_blocklist.txt",
    # Ionotter's BlockTube list (channel IDs, includes surasshu.com's AI music blocklist)
    "https://pastebin.com/raw/QS93tnXk",
)

_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,}$")
# Compacted handles shorter than this ("@music", "@jazz", "@c2c") collide with real artists.
_MIN_HANDLE_MATCH_LEN = 6


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def _name_key(name: str) -> str:
    return " ".join(name.casefold().split())


def parse_shared_list(text: str) -> Tuple[Set[str], Set[str]]:
    """Returns (channel_ids, compacted_handles) from a list in AiSList/BlockTube style: one entry
    per line. Anything else — comments, prose, URLs — is ignored rather than guessed at."""
    ids: Set[str] = set()
    handles: Set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if _CHANNEL_ID.match(line):
            ids.add(line)
        elif _HANDLE.match(line):
            key = _compact(line[1:])
            if len(key) >= _MIN_HANDLE_MATCH_LEN and re.search(r"[a-z]", key):
                handles.add(key)
    return ids, handles


@dataclass(frozen=True)
class BlockedArtist:
    name: str
    id: Optional[str] = None


class ArtistBlocklist:
    def __init__(self, directory: Optional[Path] = None):
        self._directory = directory or config_directory()
        self._blocked: List[BlockedArtist] = []
        self._allow_ids: Set[str] = set()
        self._allow_names: Set[str] = set()
        self._shared_ids: Set[str] = set()
        self._shared_handles: Set[str] = set()
        self._local_unreadable = False
        self.load_error: Optional[str] = None

    @property
    def local_path(self) -> Path:
        return self._directory / LOCAL_FILE_NAME

    @property
    def shared_path(self) -> Path:
        return self._directory / SHARED_FILE_NAME

    @property
    def blocked(self) -> Sequence[BlockedArtist]:
        return tuple(self._blocked)

    @property
    def shared_count(self) -> int:
        return len(self._shared_ids) + len(self._shared_handles)

    def load(self) -> "ArtistBlocklist":
        self.load_error = None
        self._local_unreadable = False
        try:
            if self.local_path.exists():
                data = json.loads(self.local_path.read_text())
                self._blocked = [
                    BlockedArtist(name=a.get("name") or "", id=a.get("id") or None)
                    for a in data.get("blocked", [])
                    if isinstance(a, dict) and (a.get("id") or a.get("name"))
                ]
                allow = data.get("allow", [])
                self._allow_ids = {a for a in allow if isinstance(a, str) and _CHANNEL_ID.match(a)}
                self._allow_names = {_name_key(a) for a in allow if isinstance(a, str) and not _CHANNEL_ID.match(a)}
        except (OSError, ValueError, AttributeError) as ex:
            # A hand-edited file with a typo shouldn't stop the app from starting — but it
            # mustn't be overwritten by the next block either (see _save).
            self.load_error = f"Couldn't read {self.local_path}: {ex}"
            self._local_unreadable = True

        try:
            if self.shared_path.exists():
                self._shared_ids, self._shared_handles = parse_shared_list(self.shared_path.read_text())
        except OSError as ex:
            self.load_error = f"Couldn't read {self.shared_path}: {ex}"
        return self

    def _check_writable(self) -> None:
        if self._local_unreadable:
            raise OSError(f"{self.local_path} couldn't be read at startup — fix it and restart first")

    def _save(self) -> None:
        ensure_config_directory()
        data = {
            "blocked": [{"id": a.id, "name": a.name} for a in self._blocked],
            "allow": sorted(self._allow_ids) + sorted(self._allow_names),
        }
        self.local_path.write_text(json.dumps(data, indent=2) + "\n")

    def _is_allowed(self, artist: ArtistRef) -> bool:
        return (artist.id is not None and artist.id in self._allow_ids) or _name_key(artist.name) in self._allow_names

    def _artist_blocked(self, artist: ArtistRef) -> bool:
        if self._is_allowed(artist):
            return False
        if artist.id and (artist.id in self._shared_ids or any(b.id == artist.id for b in self._blocked)):
            return True
        # A local block without a channel ID (the track didn't report one) falls back to an exact
        # name match; a local block *with* an ID only ever matches by ID, so blocking one
        # "Prince" doesn't take out every other artist who happens to share the name.
        name = _name_key(artist.name)
        if any(b.id is None and _name_key(b.name) == name for b in self._blocked):
            return True
        return _compact(artist.name) in self._shared_handles

    def is_blocked(self, track: Track) -> bool:
        return any(self._artist_blocked(a) for a in track.artists)

    def filter(self, tracks: Sequence[Track]) -> Tuple[List[Track], int]:
        """Returns (kept tracks, number removed)."""
        kept = [t for t in tracks if not self.is_blocked(t)]
        return kept, len(tracks) - len(kept)

    def block(self, artist: ArtistRef) -> BlockedArtist:
        self._check_writable()
        entry = BlockedArtist(name=artist.name, id=artist.id)
        if entry not in self._blocked:
            self._blocked.append(entry)
        # Explicitly blocking someone also takes them back off the allow list.
        if artist.id:
            self._allow_ids.discard(artist.id)
        self._allow_names.discard(_name_key(artist.name))
        self._save()
        return entry

    def unblock(self, entry: BlockedArtist) -> None:
        self._check_writable()
        if entry in self._blocked:
            self._blocked.remove(entry)
            self._save()


def update_shared_list(directory: Optional[Path] = None, sources: Sequence[str] = DEFAULT_SHARED_SOURCES) -> List[Tuple[str, Optional[int], Optional[str]]]:
    """Downloads each source, merges them and writes shared_blocklist.txt. Returns one
    (url, entries, error) row per source. Sources that fail are skipped; if every source fails
    the existing file is left alone."""
    import httpx

    target = (directory or config_directory()) / SHARED_FILE_NAME
    report: List[Tuple[str, Optional[int], Optional[str]]] = []
    ids: Set[str] = set()
    handles: Set[str] = set()
    # Handles are kept in their original "@Handle" form in the file so it stays readable.
    handle_lines: Set[str] = set()

    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "laulja"}) as client:
        for url in sources:
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as ex:
                report.append((url, None, str(ex)))
                continue
            text = response.text
            src_ids, src_handles = parse_shared_list(text)
            ids |= src_ids
            handles |= src_handles
            handle_lines |= {
                line.strip() for line in text.splitlines()
                if _HANDLE.match(line.strip()) and _compact(line.strip()[1:]) in src_handles
            }
            report.append((url, len(src_ids) + len(src_handles), None))

    if not ids and not handles:
        return report

    ensure_config_directory()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = [f"! laulja shared blocklist — downloaded {stamp}", *(f"! source: {url}" for url, n, _ in report if n is not None), ""]
    body = sorted(ids) + sorted(handle_lines, key=str.casefold)
    tmp = target.with_suffix(".tmp")
    tmp.write_text("\n".join(header + body) + "\n")
    tmp.replace(target)
    return report
