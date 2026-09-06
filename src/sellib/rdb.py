"""
Read and extract an RDB (AcSELerator QuickSet), using olefile.

An RDB is an OLE compound document with this structure:

    Relays/
        <relay name>/
            Misc/
                GL1.gle, GL2.gle, ...
                Cfg.txt, Device.txt, ...
            SET_*.TXT, BAY_SCREEN.TXT, ...

This module takes the raw bytes of an RDB, extracts every stream preserving
the hierarchy, and lists the relays with their GLE files.

The extraction lives in a cache keyed by CONTENT (see `sellib.rdb_cache`),
not in a per-tool directory keyed by name. Two identical files are the same
file: name collisions stopped existing, and the extraction is reused across
sessions and across restarts.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import olefile

from sellib import _paths, rdb_cache
from sellib.models import relay_models

_logger = logging.getLogger(__name__)

# Letters, digits, dot, hyphen, underscore and space survive in a name that
# becomes a filesystem path. Everything else becomes "_".
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._\- ]")


# A per-family fallback, used ONLY when the model registry has no entry for
# the specific model. The source of truth is the model's own JSON
# (`ip_address.file`); what is here are conservative defaults based on where
# IPADDR usually appears for each SEL family.
RELAY_FAMILY_IP_FILE: dict[str, str] = {
    "3xx": "set_p5.txt",
    "4xx": "set_p5.txt",   # SEL-411L, SEL-487E, etc.
    "7xx": "set_p1.txt",   # SEL-751, SEL-787, etc.
}

_RELAYTYPE_RE = re.compile(r"RELAYTYPE\s*=\s*(.+)", re.IGNORECASE)
_MODEL_RE = re.compile(r"SEL-?\s*([0-9][0-9A-Za-z\-]*)", re.IGNORECASE)
# Matches "IPADDR,..." but NOT "IPADDRE,...". Captures the IPv4, without any
# CIDR suffix.
_IPADDR_RE = re.compile(
    r'^\s*IPADDR\s*,\s*"?(\d{1,3}(?:\.\d{1,3}){3})',
    re.IGNORECASE | re.MULTILINE,
)


def _read_relay_model(relay_dir: Path) -> str | None:
    cfg = relay_dir / "Misc" / "Cfg.txt"
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="latin-1", errors="ignore")
    except OSError:
        return None
    m = _RELAYTYPE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).strip()        # ex.: "SEL-487E-3"
    mo = _MODEL_RE.search(raw)
    return mo.group(1) if mo else raw


def _family_from_model(model: str | None) -> str | None:
    """487E -> '4xx', 751 -> '7xx'. None when the leading character is not a digit."""
    if not model:
        return None
    first = model.lstrip().lstrip("-")[:1]
    return f"{first}xx" if first.isdigit() else None


def _find_case_insensitive(directory: Path, filename: str) -> Path | None:
    target = filename.lower()
    if not directory.is_dir():
        return None
    for child in directory.iterdir():
        if child.is_file() and child.name.lower() == target:
            return child
    return None


def _resolve_ip_file(model: str | None) -> tuple[str | None, str | None]:
    """Resolve the (filename, key) that holds the IPADDR. The model's own entry
    wins; the per-family fallback is used only when there is none."""
    rm = relay_models.lookup(model or "")
    if rm is not None and rm.ip_address_file:
        return rm.ip_address_file, (rm.ip_address_key or "IPADDR")
    fam = _family_from_model(model)
    if fam:
        f = RELAY_FAMILY_IP_FILE.get(fam)
        if f:
            return f, "IPADDR"
    return None, None


def _read_relay_ip(relay_dir: Path, model: str | None) -> str | None:
    fname, _ = _resolve_ip_file(model)
    if not fname:
        return None
    fpath = _find_case_insensitive(relay_dir, fname)
    if fpath is None:
        return None
    try:
        text = fpath.read_text(encoding="latin-1", errors="ignore")
    except OSError:
        return None
    m = _IPADDR_RE.search(text)
    return m.group(1) if m else None


def _relay_meta(extract_dir: Path, relay_name: str) -> tuple[str | None, str | None]:
    relay_dir = extract_dir / "Relays" / relay_name
    model = _read_relay_model(relay_dir)
    ip = _read_relay_ip(relay_dir, model)
    return model, ip


#: What a path component may never be. `..` climbs out of the extraction, a
#: separator invents a level, and NUL truncates the path at the syscall.
_TRAVERSAL = frozenset({"", ".", ".."})


def _safe_extract_path(target_dir: Path, entry) -> Path | None:
    """Where one OLE stream may be written, or None if it may not be written.

    `target_dir.joinpath(*entry)` took the storage path straight out of an
    uploaded compound file. Real RDBs never carry anything but ordinary names
    -- `Relays/<relay>/Misc/GL1.gle` -- but an RDB is exactly the artefact a
    commissioning engineer receives from a third party, and this is the one
    place a stranger's bytes become paths on disk.

    Legitimate names pass through BYTE FOR BYTE, which is not a nicety: the
    directory name is what `_relay_meta` looks the relay up by, and what
    `dnp_map.discover` turns back into the OLE stream path it writes to. A
    blanket `sanitize_name` here would rename the extraction out from under
    both.
    """
    parts = []
    for raw in entry:
        name = str(raw)
        if name in _TRAVERSAL or "/" in name or "\\" in name or "\x00" in name:
            name = sanitize_name(name.replace("/", "_").replace("\\", "_"))
            if name in _TRAVERSAL:
                name = "_"
        parts.append(name)
    out = target_dir.joinpath(*parts)
    try:
        out.resolve().relative_to(target_dir.resolve())
    except ValueError:
        return None
    return out


def sanitize_name(name: str) -> str:
    s = (name or "").strip()
    s = _UNSAFE_CHARS.sub("_", s)
    return s or "unknown"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: Upload read size. 1 MB is large enough that the per-call cost disappears
#: in a 140 MB file, and small enough to sit comfortably in the memory of a
#: field laptop handling several uploads at once.
UPLOAD_CHUNK = 1 << 20


def stream_to_file(source, length: int, dest: Path,
                   on_progress=None, chunk_size: int = UPLOAD_CHUNK) -> str:
    """Copy `length` bytes from `source` into `dest` and return the sha256.

    An RDB runs 40 to 140 MB and the ceiling is 500. Read all at once, that
    whole size stayed resident -- twice over, in fact, because the hash walked
    the same bytes and writing walked them again. Here memory holds one chunk
    at a time, the hash grows as the read proceeds and the file goes straight
    to disk: three passes became one.

    Reads exactly `length` bytes -- a socket `read(n)` may come back short, so
    the loop insists. Raises `ValueError` if the source runs out first.

    `on_progress(read, total, stage)` fires on every chunk, so a client's
    progress bar moves during the transfer instead of freezing on
    "processing".
    """
    if length <= 0:
        raise ValueError("arquivo vazio")
    h = hashlib.sha256()
    read_total = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        while read_total < length:
            chunk = source.read(min(chunk_size, length - read_total))
            if not chunk:
                raise ValueError(
                    f"upload interrompido: {read_total} de {length} bytes")
            out.write(chunk)
            h.update(chunk)
            read_total += len(chunk)
            if on_progress is not None:
                on_progress(read_total, length, "Recebendo arquivo")
    return h.hexdigest()


def short_sha(sha256: str) -> str:
    """The 12-char prefix every screen shows instead of a 64-char digest.

    One definition because it is an IDENTIFIER, not a display detail: the DNP
    map editor keys its per-session edits by it and the Settings Compare keys
    its RDB registry by it, so two tools disagreeing on the length would be
    two tools disagreeing on which RDB is which.
    """
    return sha256[:12]


def sha256_file(path: Path, chunk_size: int = 1 << 16) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class GleEntry:
    name: str           # ex.: "GL1"
    filename: str       # ex.: "GL1.gle"
    rel_path: str       # ex.: "Relays/QPC1_TR1_UPC1/Misc/GL1.gle"
    fs_path: Path       # absolute path, after extraction


@dataclass
class RelayEntry:
    name: str
    gles: list[GleEntry] = field(default_factory=list)
    model: str | None = None   # ex.: "487E-3" extraido de RELAYTYPE
    ip: str | None = None      # IPADDR encontrado no SET_P? da familia


@dataclass
class RdbInfo:
    rdb_path: Path
    extract_dir: Path
    sha256: str
    reused: bool                 # True when nothing had to be rewritten
    relays: list[RelayEntry]
    # The name THIS upload carried, not the cache's: stored by hash, the file
    # on disk is the same for everyone, and without this every screen would
    # show the name of whoever uploaded it first.
    display_name: str = ""


def _extract_and_collect(rdb_path: Path, target_dir: Path,
                         on_progress=None) -> list[RelayEntry]:
    """Extract every stream into `target_dir` and return the relay list.

    `on_progress(done, total, stage)` is called throughout. A real RDB holds
    thousands of streams and takes several seconds; without this a client's
    progress bar would sit on "processing" until the end.
    """
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    relays: dict[str, list[GleEntry]] = {}

    ole = olefile.OleFileIO(str(rdb_path))
    try:
        entries = ole.listdir(streams=True, storages=False)
        total = len(entries) or 1
        # Reporting on every stream would be dominated by the callback's own
        # cost; one in 64 already gives a smooth bar.
        step = max(1, total // 64)
        for i, entry in enumerate(entries):
            if on_progress is not None and (i % step == 0):
                on_progress(i, total, "Extraindo arquivos do RDB")
            out_path = _safe_extract_path(target_dir, entry)
            if out_path is None:
                _logger.warning(
                    "[rdb] stream ignorado: o caminho sai da extracao (%r)",
                    "/".join(str(e) for e in entry))
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with ole.openstream(entry) as stream:
                out_path.write_bytes(stream.read())

            # A GLE, at Relays/<relay>/Misc/<file>.gle
            if (len(entry) == 4
                    and entry[0] == "Relays"
                    and entry[2] == "Misc"
                    and entry[3].lower().endswith(".gle")):
                relay_name = entry[1]
                fname = entry[3]
                stem = fname.rsplit(".", 1)[0]
                relays.setdefault(relay_name, []).append(GleEntry(
                    name=stem,
                    filename=fname,
                    rel_path="/".join(entry),
                    fs_path=out_path,
                ))
    finally:
        ole.close()

    out: list[RelayEntry] = []
    items = sorted(relays.items())
    for i, (name, gles) in enumerate(items):
        if on_progress is not None:
            on_progress(i, len(items) or 1, "Lendo dados dos reles")
        model, ip = _relay_meta(target_dir, name)
        out.append(RelayEntry(
            name=name,
            gles=sorted(gles, key=lambda g: g.name),
            model=model,
            ip=ip,
        ))
    return out


def _scan_existing(extract_dir: Path) -> list[RelayEntry]:
    """Scan an existing extraction to find its relays and their GLE files."""
    relays_dir = extract_dir / "Relays"
    out: list[RelayEntry] = []
    if not relays_dir.is_dir():
        return out
    for relay_path in sorted(relays_dir.iterdir()):
        if not relay_path.is_dir():
            continue
        misc = relay_path / "Misc"
        gles: list[GleEntry] = []
        if misc.is_dir():
            for gle_path in sorted(misc.iterdir()):
                if gle_path.is_file() and gle_path.suffix.lower() == ".gle":
                    rel = f"Relays/{relay_path.name}/Misc/{gle_path.name}"
                    gles.append(GleEntry(
                        name=gle_path.stem,
                        filename=gle_path.name,
                        rel_path=rel,
                        fs_path=gle_path,
                    ))
        if gles:
            model, ip = _relay_meta(extract_dir, relay_path.name)
            out.append(RelayEntry(
                name=relay_path.name, gles=gles, model=model, ip=ip,
            ))
    return out


def _safe_rdb_name(filename: str) -> str:
    safe_name = sanitize_name(filename)
    if not safe_name.lower().endswith(".rdb"):
        safe_name = safe_name + ".rdb"
    return safe_name


def process_upload_stream(source, length: int, filename: str,
                          cache_root: Path | None = None,
                          on_progress=None) -> RdbInfo:
    """`process_upload`, reading from a stream instead of a `bytes` in memory.

    Same contract and same content-addressed cache; the difference is that the
    RDB's 40-140 MB never stay resident. `source` is any object with
    `.read(n)` -- over HTTP it is the request's `rfile`.

    That changes the order of things: the sha256 only exists AFTER everything
    has been read, so the file goes first into a temporary beside the cache,
    and only then do we learn whether that content was already extracted. If
    it was, the temporary is discarded; if not, it is moved into place with
    `os.replace` -- same filesystem, so the swap is atomic and a half-written
    source file never exists for another session to find.
    """
    def _report(done, total, stage):
        if on_progress is not None:
            on_progress(done, total, stage)

    if length <= 0:
        raise ValueError("arquivo RDB vazio")

    safe_name = _safe_rdb_name(filename)
    base = Path(cache_root) if cache_root is not None else _paths.cache_dir()
    incoming = base / "_incoming"
    incoming.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(incoming), suffix=".rdb-part")
    os.close(fd)
    tmp: Path | None = Path(tmp_name)
    try:
        sha = stream_to_file(source, length, Path(tmp_name),
                             on_progress=on_progress)
        entry = rdb_cache.entry_for(sha, root=cache_root)

        # The lock is per hash: two callers uploading the same file at the
        # same time used to extract over each other. The second waits and
        # reuses the result.
        with rdb_cache.lock_for(sha):
            relays: list[RelayEntry] = []
            reused = False
            if entry.complete:
                _report(0, 1, "Reaproveitando extracao existente")
                relays = _scan_existing(entry.extract_dir)
                # Edge case: the expected extraction exists but is empty or
                # incomplete.
                reused = bool(relays)
            if not reused:
                _report(0, 1, "Gravando RDB em disco")
                entry.root.mkdir(parents=True, exist_ok=True)
                # Sem meta.json ate a extracao terminar: se o processo morrer no
                # meio, a proxima passada refaz em vez de servir meia extracao.
                entry.meta_path.unlink(missing_ok=True)
                os.replace(Path(tmp_name), entry.rdb_path)
                tmp = None                    # o temporario virou o definitivo
                relays = _extract_and_collect(entry.rdb_path, entry.extract_dir,
                                              on_progress)
                rdb_cache.write_meta(entry, safe_name, len(relays))
            else:
                rdb_cache.touch(entry)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    return RdbInfo(
        rdb_path=entry.rdb_path,
        extract_dir=entry.extract_dir,
        sha256=sha,
        reused=reused,
        relays=relays,
        display_name=safe_name,
    )


def process_upload(data: bytes, filename: str, cache_root: Path | None = None,
                   on_progress=None) -> RdbInfo:
    """Extract the RDB into the content-addressed cache and report what it holds.

    The file goes to `<cache>/<sha256>/source.rdb` and the extraction to
    `.../extracted/`. Two uploads of the same content -- by the same caller or
    another, today or after a restart -- reuse the same extraction.
    `cache_root` overrides the cache root; None uses whatever
    `sellib.configure` was given.

    `display_name` comes from the name THIS upload carried, not from the
    cache, or everyone would see the name of whoever uploaded it first.

    `on_progress(done, total, stage)` feeds a client's progress bar through
    the slow phases (hashing, writing and extracting).

    This takes the bytes ready in hand. A caller holding a stream -- an HTTP
    upload -- should use `process_upload_stream`, which never loads the whole
    file. This one stays because several callers ALREADY have the bytes (an
    export's output, the matchers, the tests), and for those a BytesIO is the
    honest path.
    """
    if not data:
        raise ValueError("arquivo RDB vazio")
    return process_upload_stream(io.BytesIO(data), len(data), filename,
                                 cache_root=cache_root, on_progress=on_progress)


def find_gle(info: RdbInfo, relay_name: str, gle_name: str) -> GleEntry | None:
    """Resolve (relay, gle) -> GleEntry; aceita 'GL1' ou 'GL1.gle' em gle_name."""
    for r in info.relays:
        if r.name != relay_name:
            continue
        for g in r.gles:
            if g.name == gle_name or g.filename == gle_name:
                return g
        return None
    return None


def relays_to_dict(relays: list[RelayEntry]) -> list[dict]:
    """Serialise the relay list for JSON (what a front end consumes)."""
    return [
        {
            "name": r.name,
            "model": r.model,
            "ip": r.ip,
            "gles": [
                {"name": g.name, "filename": g.filename, "rel_path": g.rel_path}
                for g in r.gles
            ],
        }
        for r in relays
    ]
