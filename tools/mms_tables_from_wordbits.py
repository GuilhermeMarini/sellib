#!/usr/bin/env python3
"""Split the ICD-derived map into the per-part tables the GLV ships.

    python3 tools/mms_tables_from_wordbits.py            # corpus parts only
    python3 tools/mms_tables_from_wordbits.py --all      # every part (9.0 MB)

Source is `fixtures/ICD files/SEL/wordbits.json`, which is NOT in git (231 MB
of vendor ICDs behind it). The output IS in git, because the app needs it at
runtime and a clean clone has no ICDs.

TWO sources, and the second one exists because the first cannot reach it.
`wordbits.json` is built by ANOTHER project (`61850Stack/commissioning-app`),
whose own `sAddr` walk keeps only `db:NOME` and drops the DECORATED form --
`db:52A|52B?0:1:2:3`, one 61850 point carrying two Relay Word bits. Fixing
this repository's parser does not reach that file, so the decorated half is
read straight out of the ICD named by each model entry's own `path`: the same
file the plain rows came from, so no revision is ever mixed. `wordbits.json`
is never rewritten, and the plain rows come out identical.

A decorated row is `[ld_suffix, item, [alternativas, indice, nbits]]`. The
third element is optional -- the tables already published keep two -- and a
plain row always wins over a decorated one for the same bit, which is the same
rule the live SCD path applies in `mms_tables.da_rank`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from py61850 import fc_read_rank as _fc_rank  # noqa: E402
from py61850.scl import SclDocument  # noqa: E402

from sellib._paths import PACKAGE_DATA  # noqa: E402
from sellib.scl.read import sel_short_addresses  # noqa: E402

# The factory ICD corpus is not in any repository (231 MB of vendor
# files); point SELLIB_ICD_FIXTURES at a local copy.
FIXTURES_DIR = Path(os.environ.get("SELLIB_ICD_FIXTURES", "fixtures"))
MMS_MAP_OUT = PACKAGE_DATA / "mms_map"  # noqa: E402

CORPUS = {"411L", "451", "487E", "311C1", "751", "2414", "2440"}

# The FC ranking lives in py61850 (`core/fc.py`), not here and not in sellib:
# an FC is the same FC whether it was read out of a file or matched against a
# live GetLogicalDeviceDirectory, so this generator and PAC CT's live-SCD
# resolver import ONE definition instead of keeping copies that drift apart
# silently. It ranks the full 61850-7-2 set; sellib's old tuple named 6 of
# the 13, and the corpus uses 12.


def norm(part: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (part or "").upper())


def _fc(item: str) -> str:
    """The functional constraint is the second `$`-separated field of an MMS
    item (`PLT1GGIO1$ST$Ind01$stVal` -> `ST`)."""
    parts = item.split("$")
    return parts[1] if len(parts) > 1 else ""


def _collapse(rows: list) -> dict:
    """One `(ld, item)` per bit, chosen by an explicit, deterministic rule.

    Several 61850 points can share one `sAddr` -- SEL mirrors a status point
    onto every logical device and breaker node that cares about it, so a bit
    like LOCSTA in 451/010 legitimately backs 28 different items. They are
    all fed from the same Relay Word bit and therefore all read the same
    value: there is no "correct" point to pick among them when they share an
    FC, only a predictable one. But a `CO` point is not a reading of the bit
    at all -- it is the command that sets it -- so the choice is not a bare
    tiebreak, it is a ranked preference:

      1. Prefer by functional constraint, in `FC_PREFERENCE` order (`ST`
         before `MX` before ... before `CO` last). A bit that exists only as
         `CO` -- a remote-bit-style control point with no status
         representation, e.g. RB01 -- still gets its `CO` item; ranking last
         is not filtering out.
      2. Within one FC, take the lexicographically lowest (ld, item), which
         also tends to keep a bit in a predictable logical device (fewer,
         packed LD/FC containers means fewer round trips per poll cycle --
         30 requests / ~180 ms for a whole diagram on the bench 451).
    """
    by_bit: dict = {}
    for row in rows:
        by_bit.setdefault(row["bit"].upper(), []).append((row["ld"], row["item"]))
    chosen = {}
    for bit, candidates in by_bit.items():
        best = min(candidates, key=lambda c: (_fc_rank(_fc(c[1])), c))
        chosen[bit] = list(best)
    return chosen


def decorated_rows(scl_path: Path) -> dict:
    """`{BIT: [ld_suffix, item, [alternatives, index, nbits]]}` from one ICD.

    DECORATED addresses only -- the ones `wordbits.json` does not carry. The
    FC comes from the file's own `DataTypeTemplates` rather than from a
    constant: measured over the corpus's 146 ICDs, all 2,030 decorated
    addresses are `ST`, and resolving anyway is what makes a future ICD that
    disagrees fail loudly instead of writing an item the relay does not serve.

    A point whose FC does not resolve produces no row: guessing yields an item
    that disappears quietly later, when the live resolver checks it against
    the relay's own directory.
    """
    # `parse()` and not `load()` on purpose -- the strictness is the point
    # here, an ICD that will not parse must stop the generator rather than
    # produce a table missing whatever was in it.
    #
    # One walk, where there used to be two. `short_addresses` and `da_fcs`
    # were separate passes over the same file, joined on a four-part key,
    # because the sAddr walk could not see the type chain; py61850's model
    # resolves the chain once and every attribute carries its own FC, so the
    # point arrives complete and `da_fcs` has no reason to exist.
    doc = SclDocument.parse(scl_path)
    best: dict = {}
    for _ied, points in sel_short_addresses(doc).items():
        for bit, p in points.items():
            rule = p.rule
            if rule is None:
                continue
            if not p.fc:
                continue
            da = "$".join(p.da.split("."))
            item = f"{p.ln}${p.fc}${p.do}${da}"
            key = (_fc_rank(p.fc), p.ld_inst, item)
            if bit in best and key >= best[bit][0]:
                continue
            best[bit] = (key, [p.ld_inst, item,
                              [list(rule.alternatives), rule.index,
                               rule.nbits]])
    return {bit: row for bit, (_key, row) in best.items()}


def merge_decorated(plain: dict, decorated: dict) -> dict:
    """The plain rows, plus the decorated ones they do not already cover.

    Never overwrites: a bit with a boolean address keeps it. That is the same
    preference the live path applies (`mms_tables.da_rank`), and it is also
    what stops this pass from changing the item of a row already published.
    """
    out = dict(plain)
    for bit, row in decorated.items():
        out.setdefault(bit, row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path,
                    default=FIXTURES_DIR / "ICD files" / "SEL" / "wordbits.json")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--icd-dir", type=Path,
                    default=FIXTURES_DIR / "ICD files" / "SEL",
                    help="onde procurar o ICD de cada modelo, pelo basename "
                         "que o proprio wordbits.json registra em `path`")
    ap.add_argument("-o", "--out", type=Path, default=MMS_MAP_OUT)
    args = ap.parse_args()

    models = json.loads(args.src.read_text(encoding="utf-8"))["models"]
    args.out.mkdir(parents=True, exist_ok=True)
    written = total = decorated_total = 0
    missing_icd: list = []
    for key, entry in models.items():
        if not entry.get("bits"):
            continue
        part, group = key.split("/", 1)
        if not args.all and norm(part) not in CORPUS:
            continue
        bits = _collapse(entry["bits"])
        # The decorated half comes from the ICD THIS model names, not from a
        # scan of the directory: that is what guarantees both halves of the
        # table come from the same firmware revision.
        icd = args.icd_dir / Path(entry.get("path", "")).name
        source = "ICD de fabrica, via fixtures/ICD files/SEL/wordbits.json"
        if entry.get("path") and icd.is_file():
            found = decorated_rows(icd)
            if found:
                bits = merge_decorated(bits, found)
                source += f"; enderecos decorados lidos de {icd.name}"
                decorated_total += len(found)
        elif entry.get("path"):
            missing_icd.append(Path(entry["path"]).name)
        doc = {
            "part": part,
            "group": group,
            "config_version": entry.get("config_version"),
            "source": source,
            # bit -> [ld_suffix, item] or, for a decorated point,
            # [ld_suffix, item, [alternatives, index, nbits]]. The item already
            # carries the FC from the ICD; the live MMS path checks it against
            # the relay's directory before using it.
            "bits": bits,
        }
        path = args.out / f"{norm(part)}-{group}.json"
        path.write_text(json.dumps(doc, separators=(",", ":"),
                                   ensure_ascii=False) + "\n", encoding="utf-8")
        written += 1
        total += path.stat().st_size
    print(f"{written} tabelas em {args.out} ({total / 1e6:.1f} MB), "
          f"{decorated_total} endereco(s) decorado(s)")
    if missing_icd:
        # Not an error: a clone without the ICDs still generates the plain
        # tables. But in silence nobody would work out why the decorated half
        # vanished between one regeneration and the next.
        print(f"AVISO: {len(missing_icd)} ICD nao encontrado(s) em "
              f"{args.icd_dir} -- sem a metade decorada: "
              f"{', '.join(sorted(missing_icd)[:5])}"
              f"{' ...' if len(missing_icd) > 5 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
