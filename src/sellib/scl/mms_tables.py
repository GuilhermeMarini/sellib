"""The shipped bit -> MMS item tables, loaded lazily and memoised.

Third per-model registry in this project, after `data/relay_models/` (GLV and
the GLE tools) and `data/wordbits/` (the DNP map's name check). They come from
different sources and drift; `tests/test_relay_models.py` fails on a model
present in one and missing from another unless the asymmetry is written down.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from sellib import _paths

_logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: dict = {}
# A FLAG, not the truth of the dictionary itself. `if _CACHE:` looks like the
# same thing and is not: with an empty directory it re-scanned the disk on
# every lookup and, worse, when one file raised mid-loop the entries already
# read stayed cached and the NEXT call returned half a registry in silence --
# the 411L found, the 751 gone.
_LOADED = False

# Ordered FC preference for collapsing several 61850 points that share one
# Relay Word bit -- or, on the live path, for asking a GetLogicalDeviceDirectory
# which FC an SCD's LN$*$DO$DA actually landed under -- down to one. `CO`
# (control) is last on purpose: it is a command that SETS a point, not a
# reading of it, so picking it over `ST` (status) or `MX` (measurement) would
# poll the wrong thing even though it happens to be reachable through the same
# bit. `tools/mms_tables_from_wordbits.py` (the fallback-table generator) and
# the PAC CT live-SCD resolver both import this same
# tuple -- they must not keep two copies that can drift apart silently.
FC_PREFERENCE = ("ST", "MX", "SP", "CF", "DC", "CO")


def fc_rank(fc: str) -> tuple:
    """Sort key for one candidate's FC: `(0, position in FC_PREFERENCE)` for
    anything that is not `CO`, `(1, 0)` for `CO`. The two-tier shape is
    deliberate -- it keeps `CO` strictly worse than every other FC, including
    one absent from `FC_PREFERENCE` (the corpus also has a handful of `SG`,
    setting-group, points), because `CO` is a command and everything else,
    named or not, is still a reading.
    """
    if fc == "CO":
        return (1, 0)
    if fc in FC_PREFERENCE:
        return (0, FC_PREFERENCE.index(fc))
    return (0, len(FC_PREFERENCE))


# -- which DA is worth reading, and which one wins when a bit has several ----
#
# The GLV paints Relay Word BITS. A point is only useful to it when the leaf
# attribute it names carries a BOOLEAN status, so this is an allowlist and not
# a denylist: measured over the whole tracked corpus (`samples/substation_demo.scd`
# plus all ten shipped `data/mms_map/*.json`), the leaf vocabulary is closed at
# 54 names, and every boolean one in it is below. Everything else is a float
# (`instMag.f`, `*.instCVal.mag.f`), a counter (`actVal`), a setting (`setVal`,
# `setTm`), a quality bitstring (`q`), a tap position (`valWTr.posVal`), an
# enumerated direction (`dirGeneral`) or a COMMAND (`Oper.ctlVal`) -- and
# `int(bool(x))` of any of those is not a bit reading, it is a fabrication.
#
# The one-segment rule does the other half of the work: every multi-segment
# path in the corpus is a float, a control or a quality, so a leaf that has to
# descend through an SDI is out by construction.
BOOLEAN_STATUS_DAS = frozenset({
    "stVal",                                  # SPS/SPC/DPS/INS/ENS
    "general",                                # ACD/ACT
    "phsA", "phsB", "phsC",                   # ACD/ACT per phase
    "phsAB", "phsBC", "phsCA",                # ACD/ACT phase-to-phase
    "neut", "res", "neg", "pos", "zer",       # ACD/ACT sequence/residual
})

# The roots of a control (the `CO` side of a DO). A command SETS a point; it is
# not a reading of it, and polling one would be asking the relay what we last
# told it rather than what it sees.
CONTROL_DA_ROOTS = frozenset({"Oper", "SBOw", "SBO", "Cancel"})


def da_parts(da: str) -> tuple:
    """`"Oper.ctlVal"` / `"Oper$ctlVal"` -> `("Oper", "ctlVal")`.

    SCL writes the descent through an SDI with '.', MMS spells every level
    with '$'; the two sources of a map use one each.
    """
    return tuple(p for p in (da or "").replace("$", ".").split(".") if p)


def is_boolean_status(da) -> bool:
    """Is this leaf a boolean the GLV can paint as a bit?"""
    parts = da_parts(da) if isinstance(da, str) else tuple(da)
    return len(parts) == 1 and parts[0] in BOOLEAN_STATUS_DAS


# -- the enumerated DAs, which become a bit ONLY with a rule ---------------
#
# A `Pos$stVal` is a DPS: its value is a Dbpos (0 intermediate, 1 off, 2 on,
# 3 bad-state), not a boolean. A `Health$stVal` is an INS, a `dirGeneral` is
# an enumeration. None of them can be read with `int(bool(x))` -- and py61850
# hands a Dbpos back as the STRING "10", whose `bool()` is True even for
# "00", which means a breaker painted closed for ever.
#
# These DAs enter the map only alongside the rule saying which bits the value
# carries (see `parse_saddr`). Measured over the corpus (the project SCD plus
# 345 factory ICDs): these are exactly the (DO, DA) pairs that get a decorated
# address, and NONE of the 127,225 plain addresses lands on one -- so
# requiring the rule takes nothing away today. It is the gate that stops an
# invented reading getting in tomorrow.
ENUM_STATUS_DAS = frozenset({
    "stVal",                                  # DPS (Pos), INS/ENS (Health, Mod, Beh)
    "dirGeneral",                             # ACD (Dir, Str)
})

# The DOs whose `stVal` is NOT boolean. `stVal` alone cannot tell an SPS
# (`Ind01$stVal`, a bit) from a DPS or an INS (`Pos$stVal`, a Dbpos;
# `Health$stVal`, an enumeration) -- the DO is what tells them apart. Without
# this list the gate would be satisfied by accident: today none of the corpus's
# 127,225 plain addresses lands on one of these DOs, but "does not happen
# today" is not the same as "cannot get through". A `Pos$stVal` read as a
# boolean with no rule is a breaker painted closed for ever, which is the
# worst mistake this toolkit can make on a commissioning screen.
ENUM_STATUS_DOS = frozenset({
    "Pos",                                    # DPC/DPS: posicao de manobra
    "Health", "Mod", "Beh", "TrBeh",          # INS/ENS: estado do IED
    "EEHealth", "PhyHealth", "ExConSt1",
})


def is_enum_do(do: str) -> bool:
    """Is this DO's `stVal` an enumeration rather than a boolean?"""
    return (do or "") in ENUM_STATUS_DOS


def is_enum_status(da) -> bool:
    """Is this a DA whose value can carry bits, but only with a rule attached?

    `stVal` is both: the DA of a boolean SPS AND that of an enumerated DPS.
    What separates them is the `sAddr` decoration, not the DA's name -- which
    is why the gate in the map resolver demands the rule, not just this test.
    """
    parts = da_parts(da) if isinstance(da, str) else tuple(da)
    return len(parts) == 1 and parts[0] in ENUM_STATUS_DAS


def da_rank(da, decorated: bool = False) -> tuple:
    """Sort key for one candidate DA of a bit: boolean status, then a decorated
    enumerated one, then anything else, then a control -- `Oper.*` strictly
    last.

    The FC preference above cannot rescue this one: an SCD names the DA, and a
    bit whose `Oper.ctlVal` was kept and whose `stVal` was thrown away never
    reaches `fc_rank` with a status candidate to choose. Measured on
    `samples/substation_demo.scd`: `LOCSTA` and 86 other bits of the IED
    `QPC1_LT2_UPC1` resolved to `Oper.ctlVal` under a plain first-wins.

    `decorated` opens a step BETWEEN the boolean tier and the rest, and it
    has to exist: a decorated `Pos$stVal` and a plain `Ind04$stVal` are both
    `is_boolean_status`, so without it the tie between them would go back to
    document order. Measured on `QPC1_LT1_UPC1`: 10 of the 33 decorated names
    also have a plain address. The step is `(0, 1)` rather than a new tier on
    purpose -- renumbering `(1,)` and `(2,)` would disturb everything that
    already compares against them.
    """
    parts = da_parts(da) if isinstance(da, str) else tuple(da)
    if parts and (parts[0] in CONTROL_DA_ROOTS
                  or parts[-1].startswith("ctlVal")):
        return (2,)
    if decorated:
        return (0, 1) if is_enum_status(parts) or is_boolean_status(parts) \
            else (1,)
    if is_boolean_status(parts):
        return (0,)
    return (1,)


# -- the `sAddr` grammar: one 61850 point can carry TWO bits ---------------
#
# SEL writes a breaker's position as ONE point (`Pos$stVal`, a DPS) whose
# value encodes two Relay Word bits:
#
#     sAddr="db:52A|52B?0:1:2:3"
#
# The alternatives are indexed by the combination of the bits, FIRST NAME AS
# THE MOST SIGNIFICANT BIT: (0,0)->0, (0,1)->1, (1,0)->2, (1,1)->3. That is
# exactly IEC 61850's Dbpos -- intermediate, off, on, bad-state -- and the
# same reading covers `52A?1:2` (a single auxiliary contact: open=1, closed=2)
# and `RELAY_EN?5:1` (a `Mod$stVal`: off=5, on=1).
#
# Measured over the whole corpus -- the project SCD plus 345 factory ICDs,
# 132,250 `db:` addresses: 127,225 plain, 4,322 with one name and 2
# alternatives, 703 with two names and 4 alternatives.
# `len(alternatives) == 2**len(names)` in 5,025 of 5,025, never more than two
# names, and the alternatives are always small integers ({0,1,2,3,5}). A shape
# that breaks the invariant is a shape nobody has seen: `parse_saddr` returns
# `None` for it rather than guessing, because the guess here is a breaker
# painted closed while it is open.

_SADDR_PREFIX = "db:"


@dataclass(frozen=True)
class BitRule:
    """How to take ONE bit out of the value of a point that carries several."""
    alternatives: tuple    # the DA's value per combination of the bits
    index: int             # which of the names this bit is
    nbits: int             # how many names the address carried


@dataclass(frozen=True)
class SaddrSpec:
    """One parsed `sAddr="db:..."`: the names it addresses and, when present, the
    alternatives that say how the value encodes them."""
    names: tuple
    alternatives: tuple | None

    def rule_for(self, index: int) -> BitRule | None:
        """A regra do bit `index`, ou `None` num endereco liso (booleano)."""
        if self.alternatives is None:
            return None
        return BitRule(alternatives=self.alternatives, index=index,
                       nbits=len(self.names))


def parse_saddr(sa: str) -> SaddrSpec | None:
    """`"db:52A|52B?0:1:2:3"` -> names `("52A", "52B")`, alternatives
    `(0, 1, 2, 3)`. `None` for anything that is not a well-formed `db:`
    address.

    Refusing is deliberate, and is not the same as failing: the caller carries
    on with the file's other thousands of addresses. What must never happen is
    an unknown shape becoming a reading.
    """
    if not sa or not sa.startswith(_SADDR_PREFIX):
        return None
    body = sa[len(_SADDR_PREFIX):]
    head, sep, tail = body.partition("?")
    names = tuple(n.strip().upper() for n in head.split("|"))
    if not all(names):
        return None
    if not sep:
        return SaddrSpec(names=names, alternatives=None)
    try:
        alternatives = tuple(int(v) for v in tail.split(":"))
    except ValueError:
        return None
    if len(alternatives) != 2 ** len(names):
        return None
    return SaddrSpec(names=names, alternatives=alternatives)


def decode_bit(rule: BitRule | None, value):
    """The value read from the relay -> `0`/`1` for THIS bit, or `None` for no
    reading.

    Two shapes arrive here, both from py61850 itself: a BIT-STRING (the Dbpos)
    comes back as the string `"10"`, and an INTEGER or enumeration comes back
    as an `int`. Anything else -- and any value matching no alternative, such
    as a Dbpos 3 (bad-state) against a `?1:2` point -- is `None`, which drops
    the bit from the payload and leaves it indeterminate on the drawing. In
    commissioning, "I could not read it" and "the relay says 0" are different
    statements.

    Never raises: a thousand other bits depend on the same polling turn.
    """
    if rule is None:
        return None
    if isinstance(value, str):
        try:
            value = int(value, 2)
        except ValueError:
            return None
    elif isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        index = rule.alternatives.index(value)
    except ValueError:
        return None
    return (index >> (rule.nbits - 1 - rule.index)) & 1


def norm_part(part: str) -> str:
    """`311C-1`, `311C1` and `311c_1` are one peca.

    The ICD file name writes the dash, the SCD's configVersion does not, and
    the RDB writes its own. Folding them is what stopped the 311C matching
    nothing and reporting 100% of its Relay Word as unaddressable.
    """
    return re.sub(r"[^A-Z0-9]", "", (part or "").upper())


@dataclass(frozen=True)
class MmsTable:
    part: str
    group: str
    config_version: str | None
    bits: dict          # BIT -> (ld_suffix, item)


def _load_one(path: Path) -> MmsTable | None:
    """One table, or ``None`` if the file will not serve. Never raises.

    The same policy `wordbits` and `relay_models` already state in their own
    loaders: one bad file must not take the whole registry down. It matters
    more here, because this registry is consulted on the connect path -- one
    corrupt `mms_map/*.json` used to take MMS mode off the air for EVERY
    model, not just the one the broken file described.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        part = norm_part(raw["part"])
        if not part:
            raise ValueError("campo 'part' vazio")
        return MmsTable(
            part=part, group=str(raw["group"]),
            config_version=raw.get("config_version"),
            bits={k.upper(): (v[0], v[1]) for k, v in raw["bits"].items()},
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError) as e:
        _logger.warning("[mms_map] %s ignorado: %s", path.name, e)
        return None


def _load() -> dict:
    global _LOADED
    with _LOCK:
        if _LOADED:
            return _CACHE
        for base in _paths.data_dirs("mms_map"):
            for path in sorted(base.glob("*.json")):
                table = _load_one(path)
                if table is None:
                    continue
                # Overlay first: a (part, group) already filled is not
                # replaced by the packaged table.
                _CACHE.setdefault(table.part, {}).setdefault(table.group, table)
        _LOADED = True
        return _CACHE


def _group_key(group: str) -> tuple:
    """Sort key for a table group. Numeric when it is a number.

    Every shipped table uses a fixed-width three-digit group (`010`, `011`),
    where a lexicographic `max()` happens to be right. It stops being right
    the day a group is four digits or loses the padding, and the failure is
    silent -- the wrong firmware's map loads and simply covers fewer bits,
    which is indistinguishable on screen from a relay that publishes less.
    """
    text = str(group)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def groups_for(part: str) -> list:
    return sorted(_load().get(norm_part(part), {}))


def lookup(part: str, group: str | None = None):
    """The table for `part`. Without a group, the newest one.

    Nearest-group is deliberate: firmware moves faster than the ICD corpus, and
    the caller verifies every item against the relay's own directory anyway.
    """
    by_group = _load().get(norm_part(part))
    if not by_group:
        return None
    if group is not None and str(group) in by_group:
        return by_group[str(group)]
    return by_group[max(by_group, key=_group_key)]


def invalidate() -> None:
    global _LOADED
    with _LOCK:
        _CACHE.clear()
        _LOADED = False
