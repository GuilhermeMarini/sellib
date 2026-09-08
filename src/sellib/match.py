"""
Cross-match the relays in an RDB against the IEDs in an SCD.

What it is for: during commissioning you hold an RDB (the relay settings, out
of AcSELerator QuickSet) and an SCD (the IEC 61850 configuration, out of
Architect). Every relay in the RDB has to correspond to an IED in the SCD, and
the names do not always agree -- so the match is made on unique identifiers.

The match keys, in priority order:

  1. IP address  -- IPADDR in the RDB's SET_P*.TXT against
                    <ConnectedAP><Address><P type="IP"> in the SCD. The
                    primary key: it matches almost always, once the relay has
                    been given the project's real address.

  2. RID (Relay ID) -- RID in the RDB's SET_G*/SET_1.txt against IED@name in
                       the SCD. The fallback, for a relay not yet commissioned
                       (still on a factory-default 192.168.x.x) whose RID the
                       engineer has already filled in.

Which file and key each identifier lives in depends on the relay model (a 4xx
uses SET_P5.TXT/SET_G1.TXT, a 7xx uses SET_P1.TXT/SET_1.TXT), and that is
declared in the model registry. Supporting a new model is adding a JSON file,
not editing this one.

Public API:

    compare_rdb_to_scd(rdb_path, scd_path) -> MatchReport
    compare_relays_to_scd(relays, extract_dir, scd_path) -> MatchReport
    MatchReport.to_dict() / .print_summary()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from py61850.scl import SclDocument

from sellib import rdb as rdb_loader
from sellib.models import relay_models

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScdIed:
    """The three fields of an SCD's IED that a cross-match needs.

    `py61850` splits these deliberately: identity is on `IedHeader`, which is
    cheap and needs no instance tree, and the address is in `Communication`,
    which names IEDs but contains none. Matching wants them together, and
    joining them is this module's business rather than a general reader's --
    an IED with several access points has several addresses, and which one
    counts as "the" IP is a question only a consumer can answer.
    """
    name: str
    ip: str | None
    relay_type: str | None


def _read_scd(scd_path: Path) -> list[ScdIed]:
    """The SCD's IEDs with their first IP, or an empty list on a bad file.

    Graceful, because the SCD came from a user: `SclDocument.load` gives None
    and a log line rather than raising, and an unreadable file is reported as
    "SCD vazio ou ilegivel" by the caller.
    """
    doc = SclDocument.load(scd_path)
    if doc is None:
        return []
    ip_by_ied = doc.communication.ip_by_ied()
    return [ScdIed(name=name, ip=ip_by_ied.get(name), relay_type=header.type)
            for name, header in doc.ied_headers.items()]


def _index_by_ip(ieds: list[ScdIed]) -> dict[str, ScdIed]:
    """{ip -> ScdIed}, for the IEDs that have one. On a duplicate address the
    first wins, and the duplicate is logged as a warning."""
    out: dict[str, ScdIed] = {}
    for ied in ieds:
        if not ied.ip:
            continue
        if ied.ip in out:
            _logger.warning(
                "SCD: IP duplicado %s em IEDs %r e %r",
                ied.ip, out[ied.ip].name, ied.name,
            )
            continue
        out[ied.ip] = ied
    return out


def _index_by_name(ieds: list[ScdIed]) -> dict[str, ScdIed]:
    """{iedName.upper() -> ScdIed}. Lookup case-insensitive."""
    return {ied.name.upper(): ied for ied in ieds if ied.name}


@dataclass(frozen=True)
class RelayIdentifiers:
    """The identifiers read out of one relay in the RDB."""
    name: str                       # the folder's name inside the RDB
    model: str | None            # ex.: "487E-3" do RELAYTYPE
    ip: str | None
    rid: str | None
    mac: str | None


@dataclass(frozen=True)
class Match:
    """One RDB<->SCD match, with a consistency verdict attached."""
    rdb_name: str
    scd_name: str
    matched_by: str                 # "ip" | "rid"
    ip: str | None
    rid: str | None
    rdb_model: str | None
    scd_type: str | None
    model_consistent: bool          # rdb_model casa com scd_type?
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "rdb_name": self.rdb_name,
            "scd_name": self.scd_name,
            "matched_by": self.matched_by,
            "ip": self.ip,
            "rid": self.rid,
            "rdb_model": self.rdb_model,
            "scd_type": self.scd_type,
            "model_consistent": self.model_consistent,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class UnmatchedRdb:
    rdb_name: str
    model: str | None
    ip: str | None
    rid: str | None
    reason: str

    def to_dict(self) -> dict:
        return {
            "rdb_name": self.rdb_name,
            "model": self.model,
            "ip": self.ip,
            "rid": self.rid,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class UnmatchedScd:
    scd_name: str
    ip: str | None
    scd_type: str | None

    def to_dict(self) -> dict:
        return {
            "scd_name": self.scd_name,
            "ip": self.ip,
            "scd_type": self.scd_type,
        }


@dataclass
class MatchReport:
    matched: list[Match] = field(default_factory=list)
    unmatched_rdb: list[UnmatchedRdb] = field(default_factory=list)
    unmatched_scd: list[UnmatchedScd] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "matched": [m.to_dict() for m in self.matched],
            "unmatched_rdb": [u.to_dict() for u in self.unmatched_rdb],
            "unmatched_scd": [u.to_dict() for u in self.unmatched_scd],
            "warnings": list(self.warnings),
        }

    def print_summary(self) -> None:
        print(f"=== RDB <-> SCD match ({len(self.matched)} matched, "
              f"{len(self.unmatched_rdb)} RDB-only, "
              f"{len(self.unmatched_scd)} SCD-only) ===\n")
        if self.matched:
            print(f"{'RDB relay':<35} {'IP':<16} {'SCD iedName':<32} {'by':<4} model?")
            print("-" * 100)
            for m in self.matched:
                flag = "ok" if m.model_consistent else "MISMATCH"
                print(f"{m.rdb_name:<35} {(m.ip or '-'):<16} "
                      f"{m.scd_name:<32} {m.matched_by:<4} {flag}")
            print()
        if self.unmatched_rdb:
            print(f"--- RDB only ({len(self.unmatched_rdb)}) ---")
            for u in self.unmatched_rdb:
                bits = []
                if u.ip:
                    bits.append(f"ip={u.ip}")
                if u.rid:
                    bits.append(f"rid={u.rid}")
                if u.model:
                    bits.append(f"model={u.model}")
                meta = ", ".join(bits) or "(no identifiers)"
                print(f"  {u.rdb_name:<35} {meta}  [{u.reason}]")
            print()
        if self.unmatched_scd:
            print(f"--- SCD only ({len(self.unmatched_scd)}) ---")
            for s in self.unmatched_scd:
                print(f"  {s.scd_name:<35} ip={s.ip or '-'}  type={s.scd_type or '-'}")
            print()
        if self.warnings:
            print(f"--- warnings ({len(self.warnings)}) ---")
            for w in self.warnings:
                print(f"  - {w}")


def _normalize_model(s: str | None) -> str:
    """`SEL-487E-3` / `SEL_487E_A` / `487E` -> `487E`. Empty when None.

    Tolerates both separators that occur in practice: `-` in the RDB's
    RELAYTYPE (`SEL-487E-3`) and `_` in the SCD's `type` attribute
    (`SEL_487E_A`). Strips the `SEL` prefix and, if a short alphanumeric
    suffix is left (a revision such as `-3` or `_A`), strips that too --
    `487E-3` and `487E_A` both become `487E`.
    """
    if not s:
        return ""
    t = s.strip().upper().replace("_", "-")
    if t.startswith("SEL-"):
        t = t[4:]
    # corta sufixo "-N" curto (revisao) -- "487E-3" -> "487E", "411L-A" -> "411L"
    if "-" in t:
        head, tail = t.rsplit("-", 1)
        if tail.isalnum() and len(tail) <= 2:
            t = head
    return t


def _model_consistent(rdb_model: str | None, scd_type: str | None) -> bool:
    a = _normalize_model(rdb_model)
    b = _normalize_model(scd_type)
    if not a or not b:
        # With nothing on one side, there is nothing to contradict.
        return True
    return a == b


def _collect_rdb_identifiers(
    relays: list[rdb_loader.RelayEntry],
    extract_dir: Path,
) -> list[RelayIdentifiers]:
    out: list[RelayIdentifiers] = []
    for r in relays:
        relay_dir = extract_dir / "Relays" / r.name
        ids = relay_models.read_identifiers(relay_dir, r.model)
        out.append(RelayIdentifiers(
            name=r.name,
            model=r.model,
            ip=ids.get("ip") or r.ip,    # fallback pro IP que rdb_loader ja leu
            rid=ids.get("rid"),
            mac=ids.get("mac"),
        ))
    return out


def compare_relays_to_scd(
    relays: list[rdb_loader.RelayEntry],
    extract_dir: str | Path,
    scd_path: str | Path,
) -> MatchReport:
    """The variant that takes an already-extracted relay list.

    For when an `RdbInfo` is already in hand and re-extracting would be
    wasteful. `extract_dir` must point at the directory holding
    `Relays/<relay>/`.
    """
    extract_dir = Path(extract_dir)
    scd_path = Path(scd_path)

    rdb_ids = _collect_rdb_identifiers(relays, extract_dir)
    ieds = _read_scd(scd_path)
    by_ip = _index_by_ip(ieds)
    by_name = _index_by_name(ieds)

    report = MatchReport()
    if not ieds:
        report.warnings.append(f"SCD vazio ou ilegivel: {scd_path}")
        for r in rdb_ids:
            report.unmatched_rdb.append(UnmatchedRdb(
                rdb_name=r.name, model=r.model, ip=r.ip, rid=r.rid,
                reason="SCD vazio",
            ))
        return report

    used_scd_names: set[str] = set()
    for r in rdb_ids:
        ied: ScdIed | None = None
        matched_by = ""
        notes: list[str] = []

        # 1) match by IP
        #
        # `used_scd_names` matters here as much as in the RID branch: the
        # match is one-to-one, and `unmatched_scd` further down is built from
        # that set. Without the guard, two RDB relays sharing an IP both
        # matched the SAME IED and the report showed two confirmed pairs --
        # hiding the duplicate instead of reporting it. A relay folder copied
        # inside the RDB, or an IPADDR nobody changed after cloning the
        # settings, is the ordinary way to end up there, and it is exactly the
        # commissioning error this tool exists to surface.
        if r.ip and r.ip in by_ip and by_ip[r.ip].name not in used_scd_names:
            ied = by_ip[r.ip]
            matched_by = "ip"
        # 2) fallback: match por RID == iedName (case-insensitive)
        if ied is None and r.rid:
            cand = by_name.get(r.rid.upper())
            if cand and cand.name not in used_scd_names:
                ied = cand
                matched_by = "rid"
                if r.ip and cand.ip and r.ip != cand.ip:
                    notes.append(
                        f"RID casou ({r.rid}) mas IPs divergem: "
                        f"RDB={r.ip} SCD={cand.ip}"
                    )
                elif r.ip and not cand.ip:
                    notes.append("RID casou mas SCD nao tem IP pro IED.")
                elif not r.ip:
                    notes.append("RDB nao tem IP; matched so pelo RID.")

        if ied is None:
            reason = _no_match_reason(r, by_ip, by_name)
            report.unmatched_rdb.append(UnmatchedRdb(
                rdb_name=r.name, model=r.model, ip=r.ip, rid=r.rid,
                reason=reason,
            ))
            continue

        used_scd_names.add(ied.name)
        ok = _model_consistent(r.model, ied.relay_type)
        if not ok:
            notes.append(
                f"modelo divergente: RDB={r.model!r} vs SCD={ied.relay_type!r}"
            )
        report.matched.append(Match(
            rdb_name=r.name,
            scd_name=ied.name,
            matched_by=matched_by,
            ip=r.ip,
            rid=r.rid,
            rdb_model=r.model,
            scd_type=ied.relay_type,
            model_consistent=ok,
            notes=tuple(notes),
        ))

    for ied in ieds:
        if ied.name in used_scd_names:
            continue
        report.unmatched_scd.append(UnmatchedScd(
            scd_name=ied.name, ip=ied.ip, scd_type=ied.relay_type,
        ))
    return report


def _no_match_reason(
    r: RelayIdentifiers,
    by_ip: dict[str, ScdIed],
    by_name: dict[str, ScdIed],
) -> str:
    bits = []
    if not r.ip and not r.rid:
        return "sem IP nem RID legiveis no RDB"
    if r.ip and r.ip not in by_ip:
        bits.append(f"IP {r.ip} nao existe em nenhum ConnectedAP do SCD")
    if r.rid and r.rid.upper() not in by_name:
        bits.append(f"RID {r.rid!r} nao bate com nenhum IED@name")
    if not bits:
        bits.append("identificadores presentes mas ja consumidos por outro match")
    return "; ".join(bits)


def compare_rdb_to_scd(
    rdb_path: str | Path,
    scd_path: str | Path,
    base_dir: str | Path | None = None,
) -> MatchReport:
    """The whole pipeline: extract the RDB (or reuse a cached extraction, keyed
    by sha256) and compare it against the SCD.

    The extraction goes into the content-addressed cache, not into a directory
    beside the file. `base_dir`, when given, becomes an alternative ROOT for
    that cache -- useful for running this in isolation. Accepts str or Path.
    """
    rdb_path = Path(rdb_path)
    data = rdb_path.read_bytes()
    info = rdb_loader.process_upload(
        data, rdb_path.name,
        cache_root=Path(base_dir) if base_dir is not None else None,
    )
    return compare_relays_to_scd(info.relays, info.extract_dir, scd_path)
