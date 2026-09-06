"""
Loader for the per-relay-model profiles.

Each `relay_models/<MODEL>.json` describes one relay model (SEL-411L,
SEL-751, ...) and carries:

  - which SET_P*.TXT holds the IPADDR, and the key to look for in it.
  - a catalogue of GLE blocks: per type, the template for the bit derived from
    the output port (PCNDTIMER -> "PCT{instance:02d}Q"), the mapping to the
    real type in the GLE's XML (PCNDTIMER -> TIMER), and the layout and
    evaluation metadata the renderer uses.

Public API:

    load_relay_models() -> dict[str, RelayModel]      (key = MODEL, upper)
    lookup(relaytype: str) -> RelayModel | None       (RELAYTYPE from Cfg.txt)
    RelayModel.ip_address_file -> str | None
    RelayModel.derived_bit_for(xml_type, instance_no, name) -> str | None
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from sellib import _paths

_logger = logging.getLogger(__name__)


# The output_bit_pattern sentinel: the bit IS the element's own
# physical_instance_name, once a leading underscore is stripped. That covers
# SYMBOL, AND, OR and friends.
PATTERN_IDENTITY = "IDENTITY"


@dataclass(frozen=True)
class AnalogAliasRule:
    """An aliasing rule for an analogue channel's name.

    Some relays (the SEL-487E, for one) call the same channel different things
    in the Relay Word and the GLE than in Fast Meter: IAS (S = winding 1) in
    the Relay Word is IA1 in Fast Meter. The rule is applied with
    `regex.fullmatch` against the GLE SYMBOL's name, uppercased; on a match,
    `template.format(...)` produces the Fast Meter name.

    What the template can use:
      - positionally: {0} is the whole match, {1..N} the regex groups (1-based,
        as in the regex), in the order of `m.groups()`.
      - the `winding` keyword: the winding letter (group 2 of the regex),
        mapped through `winding_map` (S -> "1"). If group 2 does not exist, or
        matches no key, the original letter is used.
      - any named groups of the regex (`(?P<x>...)`), as further keywords.
    """
    regex: re.Pattern
    template: str
    winding_map: tuple   # tuple[tuple[str, str], ...] (frozen-friendly)

    def apply(self, name_upper: str) -> str | None:
        m = self.regex.fullmatch(name_upper)
        if not m:
            return None
        winding_letter = ""
        try:
            winding_letter = (m.group(2) or "").upper()
        except IndexError:
            pass
        winding_num = dict(self.winding_map).get(winding_letter, winding_letter)
        try:
            return self.template.format(
                m.group(0),
                *m.groups(),
                winding=winding_num,
                **m.groupdict(),
            ).upper()
        except (KeyError, IndexError, ValueError) as e:
            _logger.warning(
                "analog alias: template %r falhou em %r: %s",
                self.template, name_upper, e,
            )
            return None


@dataclass(frozen=True)
class AnalogGroup:
    """A family of analogue SYMBOLs (AMV, PMV, MV, MAG, ...).

    Groups the SYMBOLs whose physical_instance_name names a MEASUREMENT the
    relay makes -- a continuous quantity, not a Relay Word bit. A viewer draws
    these blocks differently and shows the value inline.

    `patterns` are compiled regexes ready for `.fullmatch()`; the JSON holds
    them as strings.
    """
    key: str               # short canonical key ("AMV", "MAG", "MV", ...)
    label: str             # label exibido no painel ("Analog Math Variables")
    patterns: tuple        # tuple[re.Pattern, ...]


@dataclass(frozen=True)
class BlockDef:
    """One block type's definition. Only the fields consumed today are typed; the
    rest of the JSON (geometry, css_class and so on) sits in `extra`, for
    reading and for a future refactor that drives the renderer from here."""
    key: str                              # canonical key (e.g. "PCNDTIMER")
    gle_xml_types: tuple[str, ...]        # 1+ XML types mapping to this block
    kind: str                             # categoria ("timer", "latch", "gate"...)
    output_bit_pattern: str | None     # template / "IDENTITY" / None
    label_fallback: str | None = None
    # Fixed port labels, in the GLE's port_index order. None means the block
    # declares no labels for that side (a plain gate: AND, OR, SYMBOL). An
    # empty tuple means the JSON said explicitly that there are none. An empty
    # string item means the port exists but its pin gets no glyph -- the 7xx
    # LATCH output, where the bit's name already says enough.
    input_sublabels: tuple[str, ...] | None = None
    output_sublabels: tuple[str, ...] | None = None
    extra: dict = field(default_factory=dict, repr=False)

    @property
    def gle_xml_type(self) -> str:
        """Compatibility: the first XML type mapped, which is the preferred one."""
        return self.gle_xml_types[0] if self.gle_xml_types else self.key

    def port_label(self, side: str, index: int) -> str | None:
        """The pin label for (side, index). `side` is "input" or "output".

        Returns None when the block declares no labels for that side, leaving
        the caller free to show "in 0", "out 1" and so on. Returns "" when the
        block states explicitly that the port carries no glyph -- it exists,
        it just has no label.
        """
        seq = self.input_sublabels if side == "input" else self.output_sublabels
        if seq is None:
            return None
        if 0 <= index < len(seq):
            return seq[index]
        return None


@dataclass(frozen=True)
class IdentifierSource:
    """Where to find one of a relay's unique identifiers in the extraction.

    `file`  -> the SET_*.TXT file name (matched case-insensitively) inside
               the relay's directory.
    `key`   -> the key to look for in it ('IPADDR', 'RID', 'MAC').
               Parsing assumes `KEY,"VALUE"` lines, which is AcSELerator's
               native SET_*.TXT format.
    """
    file: str
    key: str


@dataclass(frozen=True)
class RelayModel:
    """One relay model's profile -- the contents of one JSON file."""
    model: str
    model_aliases: tuple[str, ...]
    ip_address_file: str | None    # ex.: "set_p5.txt" (case-insensitive)
    ip_address_key: str               # ex.: "IPADDR"
    blocks: dict[str, BlockDef]       # key (canonica) -> BlockDef
    # How the relay exposes its Relay Word:
    #   "target_region"        -> 4xx (411L/487E): the Fast Message TARGET
    #                             bank is authoritative; A5D1 carries only a
    #                             subset. Read with the ASCII target reader.
    #   "fast_meter_digitals"  -> 7xx (751/787): the WHOLE Relay Word is in
    #                             the A5D1 response
    #                             (numdigitalbank/digitaloffset), so the
    #                             digitals come straight from Fast Meter.
    #   "tar_digitals"         -> 3xx (311C/311L): the analogues come from
    #                             A5D1, the digitals do NOT. Measured on a
    #                             SEL-311C-1-R509: `VIEW 1:TARGET` and
    #                             `MAP 1 TARGET BL` both answer "Invalid
    #                             Command", and A5D1 advertises
    #                             numdigitalbank=111 while the DNA block comes
    #                             back as 111 rows of "*" -- not one named bit.
    #                             The digitals only come out through ASCII
    #                             `TAR <row>`, 8 named bits per round trip.
    # The default, "target_region", preserves the behaviour for a relay that
    # has no profile of its own yet.
    fast_read: str = "target_region"
    # Index reverso construido no load: xml_type -> BlockDef.
    blocks_by_xml_type: dict[str, BlockDef] = field(default_factory=dict, repr=False)
    # Families of analogue SYMBOLs (AMV/PMV on the 4xx, MV/MAG/PHASE on the
    # 7xx). The JSON's order is preserved, and it decides which group a name
    # matching more than one pattern falls into: the first wins.
    analog_groups: tuple = field(default_factory=tuple, repr=False)
    # Name aliasing rules (GLE/Relay Word -> Fast Meter). A tuple because
    # order matters: the first rule that matches wins. Empty means
    # `resolve_analog_name` is the identity.
    analog_name_aliases: tuple = field(default_factory=tuple, repr=False)
    # Further unique identifiers, beyond the IP, for cross-matching with an
    # SCD. `relay_id` is the RID/TID the engineer types into the relay, which
    # by convention usually matches the SCD's iedName. `mac_address` is the
    # unicast MAC, when the firmware exposes it in settings (rare on the 4xx).
    relay_id: IdentifierSource | None = None
    mac_address: IdentifierSource | None = None
    source_path: Path | None = field(default=None, repr=False)

    @property
    def uses_target_region(self) -> bool:
        return self.fast_read == "target_region"

    @property
    def digitals_via_tar(self) -> bool:
        """Digitals read through ASCII `TAR <row>` (the 3xx family)."""
        return self.fast_read == "tar_digitals"

    @property
    def needs_ascii_reader(self) -> bool:
        """Needs the ASCII target reader (the name -> row/bit map of the Relay Word)."""
        return self.fast_read in ("target_region", "tar_digitals")

    def identifier_sources(self) -> dict[str, IdentifierSource]:
        """Return {kind -> IdentifierSource} for every unique identifier this model
        configures. The canonical kinds are 'ip', 'rid' and 'mac'; an entry
        missing its file or key is left out.
        """
        out: dict[str, IdentifierSource] = {}
        if self.ip_address_file:
            out["ip"] = IdentifierSource(
                file=self.ip_address_file,
                key=self.ip_address_key or "IPADDR",
            )
        if self.relay_id is not None:
            out["rid"] = self.relay_id
        if self.mac_address is not None:
            out["mac"] = self.mac_address
        return out

    def analog_group_for(self, symbol_name: str) -> AnalogGroup | None:
        """The AnalogGroup matching `symbol_name`, or None when the name is a
        digital Relay Word bit. Matching is a case-insensitive fullmatch, and
        the first group whose pattern matches wins.
        """
        if not symbol_name:
            return None
        nm = symbol_name.strip().upper()
        for grp in self.analog_groups:
            for pat in grp.patterns:
                if pat.fullmatch(nm):
                    return grp
        return None

    def is_analog_symbol(self, symbol_name: str) -> bool:
        return self.analog_group_for(symbol_name) is not None

    def resolve_analog_name(self, name: str) -> str:
        """Translate a GLE/Relay Word name into its Fast Meter name.

        On a SEL-487E, IAS (winding S) in the Relay Word is IA1 in Fast Meter.
        `analog_name_aliases` are applied in order and the first match wins;
        if none matches, `name` comes back uppercased. Always case-insensitive.
        """
        if not name:
            return name
        nm = name.strip().upper()
        for rule in self.analog_name_aliases:
            mapped = rule.apply(nm)
            if mapped is not None:
                return mapped
        return nm

    def block_for_xml_type(self, xml_type: str) -> BlockDef | None:
        """Resolve an XML type ('PLT', 'LATCH', 'PCNDTIMER') to its BlockDef, or
        None when this model registers no block for that type."""
        if not xml_type:
            return None
        return (self.blocks_by_xml_type.get(xml_type)
                or self.blocks_by_xml_type.get(xml_type.upper())
                or self.blocks.get(xml_type)
                or self.blocks.get(xml_type.upper()))

    def port_label(self, xml_type: str, side: str, index: int) -> str | None:
        """The pin's fixed label (side is "input" or "output", index is the GLE's
        port_index).

        None means no label is declared, and the caller picks a fallback.
        "" means the block declares the port exists but carries no glyph.
        """
        block = self.block_for_xml_type(xml_type)
        if block is None:
            return None
        return block.port_label(side, index)

    def derived_bit_for(
        self,
        xml_type: str,
        instance: int,
        name: str = "",
    ) -> str | None:
        """Resolve the name of the bit DERIVED from a GLE element's output.

        A "derived" bit is one that does not appear as a named SYMBOL on the
        drawing but exists in the relay's Relay Word. A PCNDTIMER at instance
        3 has no "PCT03Q" SYMBOL in the GLE, yet the bit is there in the
        relay.

        - When the block has a template (`"PCT{instance:02d}Q"`), it is
          formatted with `instance`. At `instance == 0` the bit does not
          exist: a derived bit is only stable for physical_instance_number
          >= 1.
        - When the pattern is "IDENTITY" or None, this returns None. Those
          blocks (SYMBOL, AND, OR, ...) are already covered by the scan of the
          drawing's SYMBOLs -- there is no new derived bit to add.
        - An unmapped block: None.
        """
        block = self.blocks_by_xml_type.get(xml_type) or self.blocks.get(xml_type)
        if block is None or block.output_bit_pattern is None:
            return None
        pat = block.output_bit_pattern
        if pat == PATTERN_IDENTITY:
            return None
        if instance <= 0:
            return None
        try:
            return pat.format(instance=instance).upper()
        except (KeyError, ValueError, IndexError) as e:
            _logger.warning(
                "modelo %s: template invalido para %s (%r): %s",
                self.model, xml_type, pat, e,
            )
            return None


def _parse_sublabels(raw, field_name: str, source_key: str) -> tuple[str, ...] | None:
    """Accepts: None or absent -> None; list[str] -> tuple; anything else ->
    None with a warning. That last case keeps older JSON files working, the
    ones that declared "sublabels" without anything consuming them."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return tuple(("" if x is None else str(x)) for x in raw)
    _logger.warning(
        "block %s: campo %r deve ser lista de strings ou null, ignorando.",
        source_key, field_name,
    )
    return None


def _parse_block(canonical_key: str, raw: dict) -> BlockDef:
    consumed = {
        "kind", "gle_xml_type", "output_bit_pattern", "label_fallback",
        "sublabels", "output_sublabels",
    }
    # gle_xml_type takes a string or a list of them (aliases):
    #   "PLT"                -> matches XML type="PLT" only
    #   ["PLT", "LATCH"]     -> matches XML "PLT" OR "LATCH" (the same block,
    #                           in different firmwares of one model).
    raw_xml = raw.get("gle_xml_type")
    xml_types: tuple[str, ...]
    if raw_xml is None:
        xml_types = (canonical_key,)
    elif isinstance(raw_xml, (list, tuple)):
        xml_types = tuple(str(x) for x in raw_xml if str(x).strip())
        if not xml_types:
            xml_types = (canonical_key,)
    else:
        xml_types = (str(raw_xml),)
    # Historically `sublabels` meant the INPUT labels -- that is what the GLE
    # renderer does with them; `output_sublabels` came later, for symmetry.
    input_subs = _parse_sublabels(raw.get("sublabels"), "sublabels", canonical_key)
    output_subs = _parse_sublabels(
        raw.get("output_sublabels"), "output_sublabels", canonical_key,
    )
    return BlockDef(
        key=canonical_key,
        gle_xml_types=xml_types,
        kind=str(raw.get("kind") or "unknown"),
        output_bit_pattern=raw.get("output_bit_pattern"),
        label_fallback=raw.get("label_fallback"),
        input_sublabels=input_subs,
        output_sublabels=output_subs,
        extra={k: v for k, v in raw.items() if k not in consumed},
    )


def _load_one(path: Path) -> RelayModel | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _logger.warning("nao consegui ler %s: %s", path, e)
        return None
    model = str(data.get("model") or path.stem)
    aliases = tuple(str(a) for a in (data.get("model_aliases") or []))
    ip = data.get("ip_address") or {}
    ip_file = ip.get("file")
    ip_key = ip.get("key") or "IPADDR"
    relay_id_src = _parse_identifier(data.get("relay_id"), path, "relay_id")
    mac_src = _parse_identifier(data.get("mac_address"), path, "mac_address")
    blocks_raw = data.get("blocks") or {}
    blocks: dict[str, BlockDef] = {}
    by_xml: dict[str, BlockDef] = {}
    for k, v in blocks_raw.items():
        if not isinstance(v, dict):
            continue
        bd = _parse_block(k, v)
        blocks[k.upper()] = bd
        # Register every XML type alias. If two blocks collide on the same
        # XML type the last one defined wins, and a warning says so out loud.
        for xml_t in bd.gle_xml_types:
            uk = xml_t.upper()
            prev = by_xml.get(uk)
            if prev is not None and prev.key != bd.key:
                _logger.warning(
                    "relay model %s: XML type %r mapeado por %r e %r; "
                    "o ultimo (%r) vence.",
                    data.get("model") or path.stem, xml_t, prev.key, bd.key, bd.key,
                )
            by_xml[uk] = bd
    fast_read = str(data.get("fast_read") or "target_region")
    _VALID_FAST_READ = ("target_region", "fast_meter_digitals", "tar_digitals")
    if fast_read not in _VALID_FAST_READ:
        _logger.warning(
            "relay model %s: fast_read %r desconhecido; usando 'target_region'. "
            "Valores validos: %s",
            data.get("model") or path.stem, fast_read, ", ".join(_VALID_FAST_READ),
        )
        fast_read = "target_region"
    analog_groups = _parse_analog_groups(data.get("analog_symbols"), path)
    analog_name_aliases = _parse_analog_name_aliases(
        data.get("analog_name_aliases"), path,
    )
    return RelayModel(
        model=model,
        model_aliases=aliases,
        ip_address_file=ip_file,
        ip_address_key=ip_key,
        blocks=blocks,
        fast_read=fast_read,
        blocks_by_xml_type=by_xml,
        analog_groups=analog_groups,
        analog_name_aliases=analog_name_aliases,
        relay_id=relay_id_src,
        mac_address=mac_src,
        source_path=path,
    )


def _parse_identifier(raw, source: Path, field_name: str) -> IdentifierSource | None:
    """Parse `{ "file": ..., "key": ... }` into an IdentifierSource. None when
    absent or invalid. A JSON null is accepted, and says explicitly "not
    available for this model".
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _logger.warning(
            "relay model %s: campo %r deve ser objeto {file, key} ou null, "
            "ignorando.", source.name, field_name,
        )
        return None
    f = raw.get("file")
    k = raw.get("key")
    if not f or not k:
        _logger.warning(
            "relay model %s: campo %r exige 'file' e 'key' nao-vazios, "
            "ignorando.", source.name, field_name,
        )
        return None
    return IdentifierSource(file=str(f), key=str(k))


def _parse_analog_groups(raw, source: Path) -> tuple:
    """Parse the analog_symbols list into a tuple of AnalogGroup.

    Schema:
      "analog_symbols": [
        { "key": "AMV", "label": "Analog Math Variables (AMV)",
          "patterns": ["^AMV\\d+$"] },
        ...
      ]
    An invalid pattern is logged and dropped; a group left with no valid
    pattern is dropped too.
    """
    if not raw:
        return ()
    if not isinstance(raw, (list, tuple)):
        _logger.warning("relay model %s: analog_symbols deve ser lista, ignorando.",
                        source.name)
        return ()
    out: list[AnalogGroup] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        label = str(entry.get("label") or key)
        pats_raw = entry.get("patterns") or []
        if not key or not isinstance(pats_raw, (list, tuple)):
            continue
        compiled = []
        for p in pats_raw:
            try:
                compiled.append(re.compile(str(p), re.IGNORECASE))
            except re.error as e:
                _logger.warning(
                    "relay model %s: pattern %r invalido em analog_symbols[%s]: %s",
                    source.name, p, key, e,
                )
        if not compiled:
            continue
        out.append(AnalogGroup(key=key, label=label, patterns=tuple(compiled)))
    return tuple(out)


def _parse_analog_name_aliases(raw, source: Path) -> tuple:
    """Parse analog_name_aliases into a tuple of AnalogAliasRule.

    Schema:
      "analog_name_aliases": {
        "winding_map": { "S": "1", "T": "2", ... },
        "rules": [
          { "regex": "^(I[ABC])([STUWXY])$", "template": "{1}{winding}" },
          ...
        ]
      }

    A rule with an invalid regex or no template is dropped, with a warning.
    An absent or malformed block returns () rather than raising.
    """
    if not raw:
        return ()
    if not isinstance(raw, dict):
        _logger.warning(
            "relay model %s: analog_name_aliases deve ser dict, ignorando.",
            source.name,
        )
        return ()
    wmap_raw = raw.get("winding_map") or {}
    if not isinstance(wmap_raw, dict):
        wmap_raw = {}
    # Keys uppercased, values coerced to str.
    wmap_items = tuple(
        (str(k).upper(), str(v)) for k, v in wmap_raw.items() if str(k).strip()
    )
    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, (list, tuple)):
        return ()
    out: list[AnalogAliasRule] = []
    for entry in rules_raw:
        if not isinstance(entry, dict):
            continue
        rx = entry.get("regex")
        tpl = entry.get("template")
        if not rx or not tpl:
            continue
        try:
            compiled = re.compile(str(rx), re.IGNORECASE)
        except re.error as e:
            _logger.warning(
                "relay model %s: regex invalido em analog_name_aliases %r: %s",
                source.name, rx, e,
            )
            continue
        out.append(AnalogAliasRule(
            regex=compiled,
            template=str(tpl),
            winding_map=wmap_items,
        ))
    return tuple(out)


_CACHE: dict[str, RelayModel] = {}
_ALIAS_INDEX: dict[str, RelayModel] = {}
# WHICH directories the cache was built from, not merely "was it built". A
# bare boolean meant one call with an explicit `models_dir` -- a test, a tool,
# an offline generator -- filled the process-wide cache with that directory's
# contents, and every later `lookup()` from anywhere in the process answered
# out of it. The other two registries (`wordbits`, `scl.mms_tables`) both
# carry an `invalidate()`; this one had neither that nor a key.
_LOADED_FROM: tuple[str, ...] | None = None


def _normalize(key: str) -> str:
    """Normalise for lookup: uppercase, surrounding blanks stripped."""
    return (key or "").strip().upper()


def _key_variants(name: str) -> list[str]:
    """Produce the lookup variants of a RELAYTYPE.

    Cfg.txt writes RELAYTYPE as "SEL-411L-A", and the registry may key it as
    "SEL-411L-A", "411L-A", "SEL-411L" or "411L". Ordered most specific
    first.
    """
    n = _normalize(name)
    if not n:
        return []
    variants = [n]
    if n.startswith("SEL-"):
        variants.append(n[len("SEL-"):])
    elif not n.startswith("SEL-"):
        variants.append(f"SEL-{n}")
    # tira sufixos "-A", "-1" etc.: SEL-411L-A -> SEL-411L
    for base in list(variants):
        m = re.match(r"^(.*?)(-[A-Z0-9])+$", base)
        if m:
            variants.append(m.group(1))
    # De-duplicate, preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def load_relay_models(models_dir: Path | None = None,
                       force: bool = False) -> dict[str, RelayModel]:
    """Load (and memoise) every relay model JSON.

    Without `models_dir`, the host's overlay is searched first and the
    packaged files second -- in that order, and the FIRST definition of a
    model wins. That is what lets someone add a model at runtime without the
    overlay having to repeat all the others.
    """
    global _LOADED_FROM, _CACHE, _ALIAS_INDEX
    bases = ([Path(models_dir)] if models_dir is not None
             else _paths.data_dirs("relay_models"))
    key = tuple(str(b) for b in bases)
    if _LOADED_FROM == key and not force:
        return _CACHE
    cache: dict[str, RelayModel] = {}
    alias: dict[str, RelayModel] = {}
    for base in bases:
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.json")):
            m = _load_one(path)
            if m is None:
                continue
            # setdefault: the overlay comes first and must not be
            # overwritten by the packaged file for the same model.
            cache.setdefault(_normalize(m.model), m)
            for a in (m.model, *m.model_aliases):
                alias.setdefault(_normalize(a), m)
    _CACHE = cache
    _ALIAS_INDEX = alias
    _LOADED_FROM = key
    return cache


def invalidate() -> None:
    """Forget the loaded registry so the next lookup re-reads the directories.

    The counterpart of `wordbits.invalidate()` and `scl.mms_tables.invalidate()`
    -- the three registries are read the same way and drift the same way, and
    this one was the only one a host could not reset.
    """
    global _LOADED_FROM, _CACHE, _ALIAS_INDEX
    _LOADED_FROM = None
    _CACHE = {}
    _ALIAS_INDEX = {}


def lookup(relaytype: str) -> RelayModel | None:
    """Acha o RelayModel a partir do RELAYTYPE (ex.: 'SEL-411L-A')."""
    load_relay_models()
    for key in _key_variants(relaytype):
        m = _ALIAS_INDEX.get(key)
        if m is not None:
            return m
    return None


# Matches `KEY,"value"` or `KEY,value` (unquoted), as the SET_*.TXT files
# write them. Stripping an optional CIDR suffix ("/24") from an IP is the
# caller's job.
_SETTING_RE_TEMPLATE = r'^\s*{key}\s*,\s*"?([^",\r\n]+)"?'


def _read_setting(relay_dir: Path, src: IdentifierSource) -> str | None:
    """Read setting `key` from file `file` inside `relay_dir`. Both the file
    name and the key are matched case-insensitively. Returns the raw value,
    without its quotes.
    """
    target = src.file.lower()
    found: Path | None = None
    if not relay_dir.is_dir():
        return None
    for child in relay_dir.iterdir():
        if child.is_file() and child.name.lower() == target:
            found = child
            break
    if found is None:
        return None
    try:
        text = found.read_text(encoding="latin-1", errors="ignore")
    except OSError:
        return None
    pattern = re.compile(
        _SETTING_RE_TEMPLATE.format(key=re.escape(src.key)),
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1).strip()


def read_identifiers(
    relay_dir: Path,
    model: str | None,
) -> dict[str, str | None]:
    """Read every unique identifier this relay model configures.

    Returns `{kind: value}` keyed by 'ip', 'rid' and 'mac' -- each present
    only when the model's JSON declares the source AND the file exists in the
    directory. For an IP, a CIDR suffix ('/24') is stripped from the raw
    value.

    With no model for `model`, returns an empty dict.
    """
    rm = lookup(model or "")
    if rm is None:
        return {}
    out: dict[str, str | None] = {}
    for kind, src in rm.identifier_sources().items():
        val = _read_setting(relay_dir, src)
        if val and kind == "ip":
            # Aceita "192.0.2.60/24" -> "192.0.2.60"
            val = val.split("/", 1)[0].strip()
        out[kind] = val
    return out
