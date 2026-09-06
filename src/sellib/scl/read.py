"""
Read an SCD (IEC 61850 Substation Configuration Description).

An SCD is XML with this shape:

    <SCL>
      <Communication>
        <SubNetwork>
          <ConnectedAP iedName="..." apName="..."> <Address>
            <P type="IP">192.0.2.60</P>
            ...
          </Address> </ConnectedAP>
          ...
        </SubNetwork>
      </Communication>
      <IED name="QPC1_TR1_UPC1" type="SEL_487E" manufacturer="SEL" ...>
        ...
      </IED>
      ...
    </SCL>

Per IED, this module extracts the fields worth cross-matching against an RDB:
  - name           (iedName / IED@name)
  - ip             (the IED's first ConnectedAP carrying <P type="IP">)
  - relay_type     (the <IED> `type` attribute, e.g. "SEL_487E")
  - manufacturer   (the <IED> `manufacturer` attribute, e.g. "SEL")
  - description    (the <IED> `desc` attribute)
  - config_version (the `configVersion` attribute)

Parsing is namespace-aware `xml.etree.ElementTree`, and fails gracefully on
invalid XML: an empty list and a log line, never an exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from sellib.scl._xmlsafe import DtdNotAllowed, reject_dtd_in_file
from sellib.scl.mms_tables import da_rank, parse_saddr

_logger = logging.getLogger(__name__)

# Namespace padrao do SCL/IEC 61850-6.
_SCL_NS = "http://www.iec.ch/61850/2003/SCL"
_NS = {"scl": _SCL_NS}


@dataclass(frozen=True)
class IedInfo:
    """A snapshot of one IED, as the SCD describes it."""
    name: str
    ip: str | None
    relay_type: str | None
    manufacturer: str | None
    description: str | None
    config_version: str | None


def _strip_ns(tag: str) -> str:
    """{ns}LocalName -> LocalName."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_local(root: ET.Element, local_name: str):
    """Iterate elements whose local name is `local_name`, ignoring the
    namespace. Necessary because some hand-made SCDs declare none.
    """
    for el in root.iter():
        if _strip_ns(el.tag) == local_name:
            yield el


def _collect_ip_by_ied(root: ET.Element) -> dict[str, str]:
    """Read <Communication> and return {iedName: the first IP found}."""
    out: dict[str, str] = {}
    for ap in _iter_local(root, "ConnectedAP"):
        ied = ap.attrib.get("iedName") or ap.attrib.get("iedname")
        if not ied or ied in out:
            continue
        for p in _iter_local(ap, "P"):
            ptype = (p.attrib.get("type") or "").upper()
            if ptype == "IP" and (p.text or "").strip():
                out[ied] = p.text.strip()
                break
    return out


class ScdDocument:
    """One SCD or ICD, parsed once, answering every question about it.

    The five module-level functions below each did their own `ET.parse`, so a
    caller asking the same file three things paid for three parses of it.
    Measured on a real 22 MB substation SCD: `ET.parse` alone is 368 ms, and
    the VLAN Mapper -- which calls `load_scd`,
    `extract_gse_communication_map` and `extract_goose_subscriptions_by_ied`
    on one path -- spent 1406 ms, of which 1104 ms (78%) was re-parsing the
    same bytes. Through one document it is 670 ms.

    Two constructors, because the callers genuinely differ:

    - `load()` is the graceful one, for a file a USER supplied. A missing
      file or invalid XML gives `None` and a log line, never an exception --
      the behaviour `load_scd` and the two `extract_*` functions have always
      had, and the reason the web tools can hand it whatever was uploaded.
    - `parse()` is the strict one, for a file the PROJECT ships. It raises,
      which is what `sel_da_fcs` and `sel_short_addresses` have always done:
      they run in the offline table generator, where a file that will not
      parse is a reason to stop rather than to write a partial table.

    Nothing is cached between the methods: each walks the tree it is asked
    about. It is the parse that was expensive, not the walking.
    """

    __slots__ = ("root", "path")

    def __init__(self, root: ET.Element, path: Path | None = None) -> None:
        self.root = root
        self.path = path

    @classmethod
    def load(cls, scd_path: Path) -> ScdDocument | None:
        """The document, or None with a log line on any IO or parsing error."""
        p = Path(scd_path)
        if not p.is_file():
            _logger.warning("SCD nao encontrado: %s", p)
            return None
        try:
            # Before the parser, never after: a DTD's entities expand DURING
            # the parse and there is no half-way to stop at.
            reject_dtd_in_file(p)
            tree = ET.parse(str(p))
        except DtdNotAllowed as e:
            _logger.warning("SCD recusado %s: %s", p, e)
            return None
        except (OSError, ET.ParseError) as e:
            _logger.warning("erro lendo SCD %s: %s", p, e)
            return None
        return cls(tree.getroot(), p)

    @classmethod
    def parse(cls, scd_path: Path) -> ScdDocument:
        """The document, raising if it cannot be read.

        `OSError` and `ET.ParseError` as before, plus `DtdNotAllowed` for a
        file that declares a DTD.
        """
        p = Path(scd_path)
        reject_dtd_in_file(p)
        return cls(ET.parse(str(p)).getroot(), p)

    # -- what an SCD can be asked -------------------------------------------

    def ieds(self) -> list[IedInfo]:
        """The IEDs with their identifying fields. See `load_scd`."""
        return _ieds_from_root(self.root)

    def gse_communication_map(self) -> dict[tuple[str, str, str], GseAddress]:
        """`{(publisher_ied, ld_inst, cb_name): GseAddress}`. See
        `extract_gse_communication_map`."""
        return _gse_map_from_root(self.root)

    def goose_subscriptions_by_ied(self) -> dict[str, list[GooseSubscription]]:
        """`{ied_name: [GooseSubscription, ...]}`. See
        `extract_goose_subscriptions_by_ied`."""
        return _goose_subs_from_root(self.root)

    def goose_rx_status_by_ied(self) -> dict[str, dict[str, GooseRxStatus]]:
        """`{ied_name: {BIT: GooseRxStatus}}`. See
        `extract_goose_rx_status_by_ied`."""
        return _rx_status_from_root(self.root)

    def da_fcs(self) -> dict:
        """`{IED: {(ld_inst, ln, do, da): fc}}`. See `sel_da_fcs`."""
        return _da_fcs_from_root(self.root)

    def short_addresses(self) -> dict:
        """`{ied_name: {BIT_NAME: ScdPoint}}`. See `sel_short_addresses`."""
        return _short_addresses_from_root(self.root)


def load_scd(scd_path: Path) -> list[IedInfo]:
    """Parse an SCD and return its IEDs with their identifying fields.

    Returns an empty list, and logs, on any IO or parsing error.

    Reading more than one thing out of the same file? Use `ScdDocument`: this
    parses on every call, and on a real 22 MB SCD the parse is 368 ms against
    about 100 ms of actual walking.
    """
    doc = ScdDocument.load(scd_path)
    return [] if doc is None else doc.ieds()


def _ieds_from_root(root: ET.Element) -> list[IedInfo]:
    ip_by_ied = _collect_ip_by_ied(root)
    ieds: list[IedInfo] = []
    seen: set[str] = set()
    for el in _iter_local(root, "IED"):
        name = el.attrib.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        ieds.append(IedInfo(
            name=name,
            ip=ip_by_ied.get(name),
            relay_type=el.attrib.get("type"),
            manufacturer=el.attrib.get("manufacturer"),
            description=el.attrib.get("desc"),
            config_version=el.attrib.get("configVersion"),
        ))
    return ieds


def index_by_ip(ieds: list[IedInfo]) -> dict[str, IedInfo]:
    """{ip -> IedInfo}, for the IEDs that have an IP. On a duplicate address the
    first wins, and the duplicate is logged as a warning.
    """
    out: dict[str, IedInfo] = {}
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


def index_by_name(ieds: list[IedInfo]) -> dict[str, IedInfo]:
    """{iedName.upper() -> IedInfo}. Lookup case-insensitive."""
    return {ied.name.upper(): ied for ied in ieds if ied.name}


# -----------------------------------------------------------------------------
# GOOSE / VLAN extraction
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class GseAddress:
    """The GOOSE address of one <GSE> under <ConnectedAP iedName=...>.

    Identifies a GOOSE Control Block uniquely by (publisher_ied, ld_inst,
    cb_name). The address fields come from the <Address><P type=...> block.
    """
    publisher_ied: str          # iedName do <ConnectedAP> que contem o <GSE>
    ld_inst: str                # atributo `ldInst` do <GSE>
    cb_name: str                # atributo `cbName` do <GSE>
    mac_address: str | None  # P type="MAC-Address"
    appid: str | None        # P type="APPID"
    vlan_id: str | None      # P type="VLAN-ID" (string -- hex/decimal varia)
    vlan_priority: str | None  # P type="VLAN-PRIORITY"


@dataclass(frozen=True)
class GooseSubscription:
    """One GOOSE subscription made by an IED (an <ExtRef serviceType="GOOSE">).

    Points at the GOOSE Control Block (publisher_ied, src_ld_inst,
    src_cb_name). It may or may not resolve to a GseAddress in
    <Communication> -- a subscription with no matching GSE is kept, so the
    mismatch can be reported rather than lost.
    """
    publisher_ied: str
    src_ld_inst: str
    src_cb_name: str
    desc: str | None = None         # `desc` do ExtRef (informativo)
    int_addr: str | None = None     # `intAddr` do ExtRef (informativo)
    # The bit that receives THIS subscription's health, when SEL Architect
    # declared one (`pubRxStatus`). `None` when the subscription maps no
    # health bit, and always `None` for an SCD with no SEL private block.
    # See `GooseRxStatus`.
    rx_status_bit: str | None = None


@dataclass(frozen=True)
class GooseRxStatus:
    """The bit that receives the HEALTH of one GOOSE subscription.

    SEL Architect declares it as `pubRxStatus` on the
    `<esel:GooseSubscription>` elements inside
    `<Private type="SEL_GooseSubscription">`. The bit goes to 1 when the
    publisher stops arriving, so it is not a signal out of the dataset --
    reading it as one is how a GOOSE-health bit ends up presented as a
    measurement nobody publishes.

    Its `<ExtRef>` twin is recognisable on its own: publisher and control
    block filled in, `doName` and `daName` both absent, because there is no
    data attribute to point at. That shape agrees with `pubRxStatus` 203/203
    in both directions across the two sample substations (60 IEDs), and it
    is asserted as an invariant in the tests -- but it is NOT used as a
    fallback here. `pubRxStatus` is the declared fact; inferring a health
    bit from a shape is the same class of guess as reading a breaker's
    position out of an undecorated Dbpos.

    `serviceType` is NOT that discriminator, and measuring it is what ruled
    it out: in `substation_demo.scd` all 202 health ExtRefs omit the
    attribute entirely, so `goose_subscriptions_by_ied` never returned them;
    in `IEC station 1.scd` the single health ExtRef carries
    `serviceType="GOOSE"` and IS returned as a subscription. Two exports of
    the same idea, disagreeing -- which is why a subscription carries
    `rx_status_bit` rather than being filtered out: whether a health bit
    shows up as a subscription is a property of the exporting tool, and a
    caller must be able to tell what it is holding either way.

    `bit` is whatever the attribute names: 197 of the corpus's 202 are
    `VBnnn`, 5 are `RBnn`. Narrowing to virtual bits is a caller's job.
    """
    bit: str
    publisher_ied: str
    src_ld_inst: str
    src_cb_name: str
    dat_set: str | None = None


def _gse_key(publisher_ied: str, ld_inst: str, cb_name: str) -> tuple[str, str, str]:
    """The canonical key of a GOOSE Control Block."""
    return (publisher_ied, ld_inst, cb_name)


def extract_gse_communication_map(scd_path: Path) -> dict[tuple[str, str, str], GseAddress]:
    """Read an SCD and return {(publisher_ied, ld_inst, cb_name): GseAddress}.

    Every <GSE ldInst=... cbName=...> under a <ConnectedAP iedName=...>
    becomes one entry, with its MAC/APPID/VLAN-ID/VLAN-PRIORITY. An absent
    optional field is None.

    Parses on every call; see `ScdDocument` to read an SCD once.
    """
    doc = ScdDocument.load(scd_path)
    return {} if doc is None else doc.gse_communication_map()


def _gse_map_from_root(
    root: ET.Element,
) -> dict[tuple[str, str, str], GseAddress]:
    out: dict[tuple[str, str, str], GseAddress] = {}
    for ap in _iter_local(root, "ConnectedAP"):
        publisher = ap.attrib.get("iedName") or ap.attrib.get("iedname") or ""
        if not publisher:
            continue
        for gse in _iter_local(ap, "GSE"):
            ld_inst = gse.attrib.get("ldInst") or ""
            cb_name = gse.attrib.get("cbName") or ""
            if not cb_name:
                continue
            params: dict[str, str] = {}
            for p_el in _iter_local(gse, "P"):
                ptype = (p_el.attrib.get("type") or "").upper()
                if ptype and (p_el.text or "").strip():
                    params[ptype] = p_el.text.strip()
            out[_gse_key(publisher, ld_inst, cb_name)] = GseAddress(
                publisher_ied=publisher,
                ld_inst=ld_inst,
                cb_name=cb_name,
                mac_address=params.get("MAC-ADDRESS"),
                appid=params.get("APPID"),
                vlan_id=params.get("VLAN-ID"),
                vlan_priority=params.get("VLAN-PRIORITY"),
            )
    return out


def extract_goose_subscriptions_by_ied(
    scd_path: Path,
) -> dict[str, list[GooseSubscription]]:
    """Read an SCD and return {ied_name: [GooseSubscription, ...]}.

    For each <IED>, take every <ExtRef serviceType="GOOSE"> that has both
    `iedName` (the publisher) and `srcCBName` (the control block) filled in.
    An ExtRef missing either is ignored: those are empty templates, common in
    an SCD exported before every connection was made.

    Duplicate subscriptions to the same (publisher, ldInst, cbName) are
    collapsed, keeping the first -- the others are just further intAddrs of
    the same dataset.

    Parses on every call; see `ScdDocument` to read an SCD once.
    """
    doc = ScdDocument.load(scd_path)
    return {} if doc is None else doc.goose_subscriptions_by_ied()


def extract_goose_rx_status_by_ied(
    scd_path: Path,
) -> dict[str, dict[str, GooseRxStatus]]:
    """Read an SCD and return {ied_name: {BIT: GooseRxStatus}}.

    For each `<IED>`, take every `<esel:GooseSubscription>` under
    `<Private type="SEL_GooseSubscription">` that declares a `pubRxStatus`,
    and key it by the bit that attribute names. An IED with no such
    declaration -- and an SCD from a tool that writes no SEL private block at
    all -- is simply absent from the result.

    A bit is declared at most once per IED: it receives the health of one
    subscription. Should a file name the same bit twice, the first wins and
    the collision is logged, because two publishers feeding one health bit is
    a configuration error worth seeing rather than a merge to perform here.

    Parses on every call; see `ScdDocument` to read an SCD once.
    """
    doc = ScdDocument.load(scd_path)
    return {} if doc is None else doc.goose_rx_status_by_ied()


def _rx_status_from_root(
    root: ET.Element,
) -> dict[str, dict[str, GooseRxStatus]]:
    out: dict[str, dict[str, GooseRxStatus]] = {}
    for ied_el in _iter_local(root, "IED"):
        ied_name = ied_el.attrib.get("name") or ""
        if not ied_name:
            continue
        bits: dict[str, GooseRxStatus] = {}
        for sub in _iter_local(ied_el, "GooseSubscription"):
            bit = (sub.attrib.get("pubRxStatus") or "").strip()
            if not bit:
                # A real subscription whose health is simply not mapped.
                continue
            if bit in bits:
                _logger.warning(
                    "%s: %s declarado como pubRxStatus mais de uma vez; "
                    "mantendo o primeiro (%s)",
                    ied_name, bit, bits[bit].src_cb_name,
                )
                continue
            bits[bit] = GooseRxStatus(
                bit=bit,
                publisher_ied=(sub.attrib.get("iedName") or "").strip(),
                src_ld_inst=(sub.attrib.get("ldInst") or "").strip(),
                src_cb_name=(sub.attrib.get("cbName") or "").strip(),
                dat_set=(sub.attrib.get("datSet") or None),
            )
        if bits:
            out[ied_name] = bits
    return out


def _goose_subs_from_root(
    root: ET.Element,
) -> dict[str, list[GooseSubscription]]:
    out: dict[str, list[GooseSubscription]] = {}
    # The health bit is declared on the SEL private block, keyed by the
    # control block it watches -- not on the ExtRef -- so it is resolved once
    # per document and looked up as each subscription is built.
    rx_by_ied = _rx_status_from_root(root)
    for ied_el in _iter_local(root, "IED"):
        ied_name = ied_el.attrib.get("name") or ""
        if not ied_name:
            continue
        rx_by_cb = {
            (s.publisher_ied, s.src_ld_inst, s.src_cb_name): s.bit
            for s in rx_by_ied.get(ied_name, {}).values()
        }
        seen: set[tuple[str, str, str]] = set()
        subs: list[GooseSubscription] = []
        for ext in _iter_local(ied_el, "ExtRef"):
            stype = (ext.attrib.get("serviceType") or "").upper()
            if stype != "GOOSE":
                continue
            pub = (ext.attrib.get("iedName") or "").strip()
            cb = (ext.attrib.get("srcCBName") or "").strip()
            if not pub or not cb:
                # A template or placeholder ExtRef: not a real subscription.
                continue
            ld = (ext.attrib.get("srcLDInst") or "").strip()
            key = _gse_key(pub, ld, cb)
            if key in seen:
                continue
            seen.add(key)
            subs.append(GooseSubscription(
                publisher_ied=pub,
                src_ld_inst=ld,
                src_cb_name=cb,
                desc=(ext.attrib.get("desc") or None),
                int_addr=(ext.attrib.get("intAddr") or None),
                rx_status_bit=rx_by_cb.get(key),
            ))
        if subs:
            out[ied_name] = subs
    return out


# -- sAddr: the Relay Word's name, inside the SCL --------------------------
#
# SEL writes the bit's name as `sAddr="db:NAME"` on the DAI. That attribute
# belongs to SCL and the relay does NOT serve it over MMS, so this is the only
# bridge between the name the GLE draws and the MMS item the relay answers to.
#
# The FC is not here: it lives on the DA of the DOType, inside
# DataTypeTemplates. This module does not resolve that chain -- on the live
# path the relay itself gives the FC, by matching `LN$*$DO$DA` against
# GetLogicalDeviceDirectory.

@dataclass(frozen=True)
class ScdPoint:
    """Where a Relay Word bit lives in the 61850 model -- everything but the FC."""
    bit: str
    ld_inst: str
    ln: str          # prefix + lnClass + inst, como o MMS soletra
    do: str
    da: str          # 'stVal', or 'Oper.ctlVal' when it comes from an SDI
    # How to take THIS bit out of the point's value, when the point carries
    # more than one (`sAddr="db:52A|52B?0:1:2:3"` on a DPS). `None` for a
    # plain address, which is the overwhelming majority -- 127,225 of the
    # corpus's 132,250. See `mms_tables.parse_saddr` and
    # `mms_tables.decode_bit`.
    rule: object | None = None


def _ln_name(ln: ET.Element) -> str:
    if _strip_ns(ln.tag) == "LN0":
        return "LLN0"
    return (f'{ln.get("prefix") or ""}{ln.get("lnClass") or ""}'
            f'{ln.get("inst") or ""}')


def _walk_dais(node: ET.Element, trail: list):
    """Yield (da_path, element) for every DAI under a DOI, descending into SDI."""
    for child in node:
        tag = _strip_ns(child.tag)
        if tag == "DAI":
            yield ".".join(trail + [child.get("name") or ""]), child
        elif tag == "SDI":
            yield from _walk_dais(child, trail + [child.get("name") or ""])


def _type_index(root: ET.Element) -> tuple:
    """`DataTypeTemplates` -> (`{lnType: {DO: DOType}}`, `{DOType: {DA: fc}}`).

    Only FIRST-level `DA`s go into the second index, because that is where the
    FC lives. An `Oper.ctlVal` inherits the `CO` of `Oper` itself, which is
    how IEC 61850 defines it: the functional constraint belongs to the root
    DA, and everything descending inside it comes along.
    """
    dos_by_lntype: dict = {}
    fcs_by_dotype: dict = {}
    for tpl in _iter_local(root, "DataTypeTemplates"):
        for lnt in _iter_local(tpl, "LNodeType"):
            dos_by_lntype[lnt.get("id")] = {
                do.get("name"): do.get("type")
                for do in lnt if _strip_ns(do.tag) == "DO"}
        for dot in _iter_local(tpl, "DOType"):
            fcs_by_dotype[dot.get("id")] = {
                da.get("name"): da.get("fc")
                for da in dot if _strip_ns(da.tag) == "DA"}
    return dos_by_lntype, fcs_by_dotype


def sel_da_fcs(scd_path: Path) -> dict:
    """`{IED: {(ld_inst, ln, do, da): fc}}`, resolved through DataTypeTemplates.

    The LIVE path does not use this and must not: there the FC comes from the
    relay itself, matching `LN$*$DO$DA` against `GetLogicalDeviceDirectory`,
    which resolves the FC and verifies the entry in one step.

    Here there is no relay: the factory table is generated offline from the
    ICDs, and the item needs its FC baked in. Measured over the corpus's 146
    ICDs, all 2,030 decorated addresses land on `ST` -- resolving anyway,
    rather than hardcoding `ST`, is what makes a future ICD that disagrees
    fail loudly instead of producing a wrong item.

    A DA that does not resolve stays OUT of the dictionary. Guessing an FC
    produces an item the relay does not serve, and then the bit disappears in
    silence much further downstream.

    Parses on every call, and unlike `load_scd` it RAISES on a bad file --
    this is the offline generator's path, where a file that will not parse is
    a reason to stop rather than to carry on with a partial table. See
    `ScdDocument.parse` to read an ICD once and ask it both this and
    `short_addresses`, which is what the generator does.
    """
    return ScdDocument.parse(scd_path).da_fcs()


def _da_fcs_from_root(root: ET.Element) -> dict:
    dos_by_lntype, fcs_by_dotype = _type_index(root)
    out: dict = {}
    for ied in _iter_local(root, "IED"):
        fcs: dict = {}
        for ldev in _iter_local(ied, "LDevice"):
            ld_inst = ldev.get("inst") or ""
            for ln in list(_iter_local(ldev, "LN0")) + list(_iter_local(ldev, "LN")):
                ln_name = _ln_name(ln)
                dos = dos_by_lntype.get(ln.get("lnType"), {})
                for doi in _iter_local(ln, "DOI"):
                    do = doi.get("name") or ""
                    by_da = fcs_by_dotype.get(dos.get(do), {})
                    for da_path, _dai in _walk_dais(doi, []):
                        # The FC belongs to the ROOT DA: `Oper.ctlVal` is
                        # `CO` because `Oper` is `CO`.
                        fc = by_da.get(da_path.split(".")[0])
                        if fc:
                            fcs[(ld_inst, ln_name, do, da_path)] = fc
        out[ied.get("name") or ""] = fcs
    return out


def sel_short_addresses(scd_path: Path) -> dict:
    """`{ied_name: {BIT_NAME: ScdPoint}}` for every sAddr="db:...".

    One name appears several times within an IED: the same bit shows up as
    `stVal` on the ST side and as `Oper.ctlVal` on the CO side, for instance.
    The one that stays is the one with the LOWEST `da_rank` -- boolean status
    first, DECORATED enumerated status next, a command last -- with document
    order breaking a tie.

    This cannot be left to `fc_rank` further downstream: that one chooses
    between the FCs of ONE DA, and by then the status candidate would already
    have been thrown away. Measured on the reference SCD: under a plain
    first-wins, `LOCSTA` and `IPRST` (among 87 points of one IED) resolved to
    `CFG/LLN0.LocSta.Oper.ctlVal` -- so a viewer would read the command
    instead of the state.

    One `sAddr` can address TWO bits in a single point -- `db:52A|52B?0:1:2:3`
    on a `Pos$stVal`, whose Dbpos encodes both auxiliary contacts. Each name
    becomes its own `ScdPoint`, carrying the `rule` that says how to take its
    bit out of the value read. The grammar, and the invariant
    `len(alt) == 2**len(names)`, live in `mms_tables.parse_saddr`, and a shape
    that breaks it is discarded rather than guessed. Before that the key
    became the literal string `52A|52B?0:1:2:3` and the whole form vanished in
    silence: 55 of the 7,524 bits drawn across a substation's 25 relays, every
    one of them a breaker or disconnector position.

    Parses on every call, and RAISES on a bad file, for the same reason
    `sel_da_fcs` does. See `ScdDocument.parse` to read a file once.
    """
    return ScdDocument.parse(scd_path).short_addresses()


def _short_addresses_from_root(root: ET.Element) -> dict:
    out: dict = {}
    for ied in _iter_local(root, "IED"):
        name = ied.get("name") or ""
        bits: dict = {}
        best: dict = {}          # BIT -> rank of the candidate currently winning
        for ldev in _iter_local(ied, "LDevice"):
            ld_inst = ldev.get("inst") or ""
            for ln in list(_iter_local(ldev, "LN0")) + list(_iter_local(ldev, "LN")):
                ln_name = _ln_name(ln)
                for doi in _iter_local(ln, "DOI"):
                    do = doi.get("name") or ""
                    for da_path, dai in _walk_dais(doi, []):
                        spec = parse_saddr(dai.get("sAddr") or "")
                        if spec is None:
                            continue
                        decorated = spec.alternatives is not None
                        rank = da_rank(da_path, decorated=decorated)
                        for i, bit in enumerate(spec.names):
                            if bit in best and rank >= best[bit]:
                                continue
                            best[bit] = rank
                            bits[bit] = ScdPoint(
                                bit=bit, ld_inst=ld_inst, ln=ln_name,
                                do=do, da=da_path, rule=spec.rule_for(i))
        out[name] = bits
    return out
