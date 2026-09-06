"""The names a relay model accepts in each DNP map block, used to warn.

The DNP map editor writes names into a relay's settings. A typo ("PSV2" for
"PSV02") is silently accepted by the RDB and only shows up as a dead point
during commissioning. This module flags those.

It only ever WARNS. There is no arrangement of data here that stops an export:
the lists trail the firmware by construction, so a name this module has never
heard of is at least as likely to be our gap as the user's mistake.

Two sources feed one file per model in ``data/wordbits/<MODEL>.json``, and they
answer different questions:

* **The SEL DNP3 Device Profile** (``sellib.dnp_profile``, seeded by
  ``tools/wordbits_from_dnp_profile.py`` from the zips in ``docs/``) gives the
  vendor's own default point list per block: ``kinds.BI``, ``kinds.BO`` and so
  on. For AO and CO this is the whole domain; for BO it is the whole domain
  once the ``close:open`` grammar is split and the Relay Word is unioned in.
* **The Relay Word** (``tools/wordbits_from_glv_cache.py``, from the GLV's own
  per-FID bit discovery) gives ``bits``: every named bit the firmware reports.
  A BI point may be mapped to any of them, so this -- not the profile -- is
  what BI is judged against.

Which blocks are judged at all is per model, and it is data, not policy: the
file's ``check_kinds`` lists exactly the blocks whose domain that file actually
covers, and every other block is left alone. The numbers behind the shipped
values, measured over the whole real RDB corpus (placeholders excluded, colon
grammar split):

    BI   profile only  28.61% outside   |  + Relay Word   0.00%
    BO   profile only   2.50% outside   |  + Relay Word   0.00%
    AI   profile only   4.39% outside   (math variables MV/AMV and fault
                                         quantities FIA/FIB are absent from
                                         the vendor's default list)
    AO   profile only   0.00% outside
    CO   profile only   0.00% outside

AI is therefore left out of ``check_kinds``: a wall of false warnings teaches
the engineer to ignore warnings, which destroys the feature rather than
delivering it. The duplicate check is domain-independent and runs on every
block regardless.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sellib import _paths

_logger = logging.getLogger(__name__)

_CACHE: dict[str, WordbitSet] | None = None

# The free-slot / placeholder markers common to every SEL DNP map, regardless
# of model. A model's own JSON can list more (its own "unused" spelling), but
# these always apply -- including, crucially, when the model has no JSON at
# all: without this, `lookup()` returning `None` silently drops the
# placeholder exclusion too, and every empty slot in an unmodeled relay
# reads as a "duplicado". See `always_valid_for()`.
DEFAULT_ALWAYS_VALID: set[str] = {"", "NA", "0", "1"}

KINDS = ("BI", "BO", "AI", "AO", "CO")

# The blocks whose values may name a Relay Word bit. BI is a bare bit name and
# BO is a `<close>:<open>` pair of them; AI names an analog register, AO and CO
# name control-select macros and counters. Unioning the Relay Word into those
# three would not widen a real domain, it would only mask typos that happen to
# collide with a bit name.
_RELAY_WORD_KINDS = frozenset({"BI", "BO"})

# A field can carry more than a name. BO writes `<close>:<open>`; AI, AO and CO
# write `<name>:<scale>:<deadband>` on the models that keep those inline rather
# than in their own AI_SCA/AI_DBD keys. Splitting is what takes BO from 2.50%
# unknown to 0.00% on the real corpus.
_MULTI_NAME_KINDS = frozenset({"BO"})

_NUMERIC = re.compile(r"^[-+]?[0-9]*\.?[0-9]+$")


def names_in(kind: str, value: str) -> list[str]:
    """The name tokens inside one raw field value, per that block's grammar.

    ``('BO', 'RB03:RB03')`` -> ``['RB03', 'RB03']``;
    ``('AI', 'IA_MAG:0.100:5')`` -> ``['IA_MAG']``;
    ``('BI', '52A')`` -> ``['52A']``.
    Purely numeric fragments are dropped -- a scale factor is not a name.
    """
    parts = [p.strip().upper() for p in (value or "").split(":")]
    if kind not in _MULTI_NAME_KINDS:
        parts = parts[:1]
    return [p for p in parts if p and not _NUMERIC.match(p)]


@dataclass
class WordbitSet:
    """What one relay model accepts, per DNP block, plus its placeholders."""

    model: str
    bits: set[str] = field(default_factory=set)          # Relay Word, upper
    always_valid: set[str] = field(default_factory=set)  # upper-cased
    patterns: list[tuple[re.Pattern, str]] = field(default_factory=list)
    # Block letter -> the profile's default point names for it.
    kinds: dict = field(default_factory=dict)
    # Block letters this file's data is complete enough to judge.
    check_kinds: frozenset = frozenset({"BI"})

    def validates(self, kind: str) -> bool:
        """Whether a warning on this block would be trustworthy."""
        return kind in self.check_kinds

    def domain(self, kind: str) -> set[str]:
        """Every name this model is known to accept in ``kind``."""
        names = set(self.kinds.get(kind, ()))
        if kind in _RELAY_WORD_KINDS:
            names |= self.bits
        return names

    def check(self, value: str, kind: str = "BI") -> str:
        """``"ok"`` or ``"unknown"``. Never raises, never blocks.

        ``"ok"`` for any block this file does not claim to judge, so a caller
        that forgets to consult ``validates()`` still cannot manufacture a
        warning out of data that does not support one.
        """
        if not self.validates(kind):
            return "ok"
        # `always_valid_for(self)`, not `self.always_valid`: the placeholder
        # set is domain-INDEPENDENT and never something a per-model file opts
        # into. The duplicate check next door already unioned the defaults in
        # and this did not, so a file that listed an `always_valid` of its own
        # lost "NA", "0", "1" and "" here alone -- and a hundred free slots
        # reading "NA" came back as a hundred unknown names, which is exactly
        # the wall of false warnings this module's docstring says destroys
        # the feature.
        allowed = always_valid_for(self)
        raw = (value or "").strip().upper()
        if raw in allowed:
            return "ok"
        names = self.domain(kind)
        tokens = names_in(kind, value)
        if not tokens:
            return "ok"
        for tok in tokens:
            if tok in allowed or tok in names:
                continue
            if any(rx.match(tok) for rx, _label in self.patterns):
                continue
            return "unknown"
        return "ok"


def duplicates(values: list[str], always_valid: set[str]) -> set[str]:
    """Bits that appear at more than one DNP index in the same block.

    Legal in DNP and occasionally deliberate; almost always a copy-paste slip.
    Placeholders are excluded -- a hundred free slots reading "NA" are not a
    hundred duplicates.
    """
    skip = {a.strip().upper() for a in always_valid}
    seen: set[str] = set()
    dup: set[str] = set()
    for raw in values:
        v = (raw or "").strip().upper()
        if not v or v in skip:
            continue
        if v in seen:
            dup.add(v)
        seen.add(v)
    return dup


def always_valid_for(wbs: WordbitSet | None) -> set[str]:
    """Placeholders to exclude from the duplicate check, for this model.

    ``DEFAULT_ALWAYS_VALID`` always applies -- it is domain-independent, not
    something a per-model file opts into -- unioned with whatever ``wbs``
    additionally allows. With no model (``wbs is None``) it is the fallback,
    not an empty set: a model with no wordbits file must not lose the
    placeholder exclusion, since that is exactly when this warning is
    loudest.
    """
    if wbs is None:
        return set(DEFAULT_ALWAYS_VALID)
    return DEFAULT_ALWAYS_VALID | wbs.always_valid


def _key_variants(relaytype: str) -> list[str]:
    """'SEL-411L-A' -> ['411L-A', '411L', 'SEL-411L-A', 'SEL-411L']."""
    raw = (relaytype or "").strip().upper()
    if not raw:
        return []
    stripped = raw[4:] if raw.startswith("SEL-") else raw
    out = [stripped, raw]
    # Strip option suffixes ("-A", "-3") one at a time until only the model remains.
    parts = stripped.split("-")
    while len(parts) > 1:
        parts = parts[:-1]
        out.append("-".join(parts))
        out.append("SEL-" + "-".join(parts))
    seen: list[str] = []
    for k in out:
        if k not in seen:
            seen.append(k)
    return seen


def _load_one(path: Path) -> tuple[WordbitSet, list[str]] | None:
    """Parse one wordbits JSON file into (set, aliases). Returns ``None`` on
    any read or shape problem -- never raises. A hand-curated file can be
    malformed in ways a schema check would not anticipate, and one bad file
    must never take the rest of the registry down with it.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _logger.warning("[wordbits] %s unreadable: %s", path.name, e)
        return None
    if not isinstance(raw, dict):
        _logger.warning("[wordbits] %s is not a JSON object; skipped",
                        path.name)
        return None
    try:
        model = str(raw.get("model", "")).strip().upper()
        if not model:
            _logger.warning("[wordbits] %s has no 'model'; skipped",
                            path.name)
            return None
        patterns: list[tuple[re.Pattern, str]] = []
        for p in raw.get("patterns", []):
            try:
                patterns.append((re.compile(p["re"], re.IGNORECASE),
                                 p.get("label", "")))
            except (KeyError, TypeError, AttributeError, re.error) as e:
                _logger.warning("[wordbits] %s: invalid pattern (%s)",
                                path.name, e)

        kinds: dict = {}
        for kind, names in (raw.get("kinds") or {}).items():
            key = str(kind).strip().upper()
            if key in KINDS:
                kinds[key] = {str(n).strip().upper() for n in names}

        # A schema-1 file predates the profile-derived per-kind lists and only
        # ever carried a Relay Word `bits` list, which is exactly BI's domain
        # -- so that is what it is still allowed to judge.
        declared = raw.get("check_kinds")
        if declared is None:
            check = frozenset({"BI"})
        else:
            check = frozenset(str(k).strip().upper() for k in declared
                              if str(k).strip().upper() in KINDS)

        wbs = WordbitSet(
            model=model,
            bits={str(b).strip().upper() for b in raw.get("bits", [])},
            always_valid={str(a).strip().upper()
                          for a in raw.get("always_valid", [])},
            patterns=patterns,
            kinds=kinds,
            check_kinds=check,
        )
        aliases = [model] + [str(a) for a in raw.get("model_aliases", [])]
        return wbs, aliases
    except (TypeError, AttributeError, KeyError, ValueError) as e:
        _logger.warning("[wordbits] %s: malformed, skipped (%s)",
                        path.name, e)
        return None


def _load_all() -> dict[str, WordbitSet]:
    index: dict[str, WordbitSet] = {}
    for base in _paths.data_dirs("wordbits"):
        for path in sorted(base.glob("*.json")):
            loaded = _load_one(path)
            if loaded is None:
                continue
            wbs, aliases = loaded
            for alias in aliases:
                for key in _key_variants(alias):
                    # Host overlay first: `setdefault` was already the rule
                    # within one directory, and with two it is what makes a
                    # user-supplied file win over the packaged one.
                    index.setdefault(key, wbs)
    return index


def invalidate() -> None:
    """Forget the loaded registry so the next lookup re-reads the directory.

    Importing a device profile writes a new file into the host's overlay while
    the server is running; without this the new model stays invisible until a
    restart, and the user is told validation is off for a relay they just
    supplied the profile for.
    """
    global _CACHE
    _CACHE = None


def loaded_models() -> list[dict]:
    """One row per distinct wordbits file, for showing what is installed."""
    if _CACHE is None:
        lookup(None)
    seen: dict[int, WordbitSet] = {}
    for wbs in (_CACHE or {}).values():
        seen.setdefault(id(wbs), wbs)
    out = []
    for wbs in seen.values():
        out.append({
            "model": wbs.model,
            "bits": len(wbs.bits),
            "kinds": {k: len(v) for k, v in sorted(wbs.kinds.items())},
            "check_kinds": sorted(wbs.check_kinds),
        })
    return sorted(out, key=lambda r: r["model"])


# -----------------------------------------------------------------------------
# Writing a file
# -----------------------------------------------------------------------------
#
# Both the offline generator (`tools/wordbits_from_dnp_profile.py`) and the
# editor's "importar perfil DNP" route produce a file, and a schema with two
# writers drifts. This is the single one.


def check_kinds_for(kinds: dict, bits: set) -> list[str]:
    """Which blocks the given data is complete enough to warn about.

    Each entry is a measurement over the real RDB corpus, not a preference --
    see the table at the top of this module. A block whose domain the data
    does not actually cover is left out, because a warning nobody can trust
    is worse than no warning at all.
    """
    out = []
    if bits:
        # BI's domain is the Relay Word, which only `bits` supplies. A device
        # profile lists the factory default BI map only (28.6% of real values
        # fall outside it), so a profile alone never enables BI.
        out.append("BI")
    if kinds.get("BO"):
        # 0.00% outside once the `<close>:<open>` pair is split; the Relay
        # Word covers the remote bits the default list omits.
        out.append("BO")
    for kind in ("AO", "CO"):
        if kinds.get(kind):
            out.append(kind)
    # AI is deliberately absent: 4.39% of real AI values (math variables
    # MV/AMV, fault quantities FIA/FIB) are outside the vendor's default list.
    return out


def entry_from_profiles(profiles: list, existing: dict | None = None,
                        merge_kinds: bool = False) -> dict:
    """A ``data/wordbits/<MODEL>.json`` body from one or more device profiles.

    ``existing`` is merged, not replaced: ``bits``, ``patterns`` and
    ``always_valid`` come from it untouched, so importing a profile can never
    discard a Relay Word harvest or a hand-tuned pattern.

    ``merge_kinds`` decides what happens to the point names already in the
    file, and the two callers genuinely want opposite things. The offline
    generator is handed every profile for the model at once, so it rebuilds
    that half from scratch and a name dropped by a newer document actually
    disappears. The editor's import route sees one uploaded bundle at a time
    and cannot know about the others, so it unions -- otherwise importing the
    787-4 document would silently narrow a file that already covered the 787.
    """
    from datetime import date

    existing = existing or {}
    kinds: dict[str, set[str]] = {k: set() for k in KINDS}
    if merge_kinds:
        for k, names in (existing.get("kinds") or {}).items():
            key = str(k).strip().upper()
            if key in kinds:
                kinds[key] |= {str(n).strip().upper() for n in names}
    aliases: list[str] = []
    sources = []
    for prof in profiles:
        for k in KINDS:
            kinds[k] |= set(prof.kinds.get(k, ()))
        for key in _profile_keys(prof):
            if key not in aliases:
                aliases.append(key)
        sources.append({
            "file": prof.source_name,
            "device_name": prof.device_name,
            "document_version": prof.document_version,
        })
    for src in existing.get("source", {}).get("dnp_profiles", []):
        if src not in sources:
            sources.append(src)

    model = base_model(profiles[0]) if profiles else \
        str(existing.get("model", "?"))
    bits = {str(b).strip().upper() for b in existing.get("bits", [])}
    # A file that lists its own `always_valid` keeps exactly that list, which
    # is what this function's docstring promises. It does NOT need the
    # defaults folded in: `always_valid_for()` unions them at READ time, for
    # every file and for no file at all, so a generated file that omits "NA"
    # still has "NA" accepted. The `or` is the "no list at all" case only.
    always = {str(a).strip().upper()
              for a in existing.get("always_valid", [])} or set(
                  DEFAULT_ALWAYS_VALID)

    return {
        "schema_version": 2,
        "model": model,
        "model_aliases": [a for a in aliases if a != model],
        "always_valid": sorted(always),
        "check_kinds": check_kinds_for(kinds, bits),
        "kinds": {k: sorted(kinds[k]) for k in KINDS},
        "bits": sorted(bits),
        "patterns": existing.get("patterns", []),
        "source": {
            "dnp_profiles": sources,
            # Provenance of `bits`, which no profile ever writes -- carried
            # over so a merged file still says where each half came from.
            # A schema-1 file predates `dnp_profiles`, so its whole `source`
            # block IS the Relay Word provenance and gets promoted once. A
            # schema-2 file already has the field, and taking `or
            # existing["source"]` when it is legitimately null would nest the
            # previous source block inside itself on every regeneration.
            "relay_word": _relay_word_source(existing),
            "generated": date.today().isoformat(),
        },
    }


def _relay_word_source(existing: dict):
    """Where ``bits`` came from, carried across a regeneration."""
    src = existing.get("source") or {}
    if "relay_word" in src:
        return src["relay_word"]
    if "dnp_profiles" in src:
        # Schema 2 written before this field existed: it had no Relay Word
        # provenance to record, and `src` describes profiles, not bits.
        return None
    # Schema 1: `source` was written by the Relay Word harvester itself.
    return src or None


def base_model(prof) -> str:
    """The model without its option suffix: '411L-A' and '411L-2' -> '411L'.

    One file per base model on purpose. SEL ships a separate profile per
    option and firmware revision, but they name the same points; keeping them
    apart would mean a 421-4 relay finding no file only because the bundle the
    user downloaded happened to be the 421-7 one.
    """
    return prof.models[0].split("-")[0] if prof.models else "?"


def _profile_keys(prof) -> list[str]:
    """Every model key one profile should answer to, most specific first."""
    out: list[str] = []
    for m in prof.models:
        for key in (m, m.split("-")[0]):
            if key and key not in out:
                out.append(key)
    return out


def lookup(relaytype: str | None) -> WordbitSet | None:
    """Find the set for a RELAYTYPE. ``None`` means validation is off."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _load_all()
    if not relaytype:
        return None
    for key in _key_variants(relaytype):
        found = _CACHE.get(key)
        if found is not None:
            return found
    return None
