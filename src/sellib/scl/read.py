"""What SEL puts inside an SCL file that no vendor-neutral reader can read.

The standard half of an SCD -- the IEDs, the `Communication` section, the
`DataTypeTemplates` pool, the per-IED instance tree, datasets, control blocks
and `ExtRef`s -- is `py61850.scl`'s, and this module holds none of it. Read a
document once with `SclDocument`, then hand it to the functions here:

    from py61850.scl import SclDocument
    from sellib.scl.read import sel_short_addresses, sel_goose_subscriptions

    doc = SclDocument.load(path)            # None + a log line on a bad file
    points = sel_short_addresses(doc)       # {ied: {BIT: ScdPoint}}
    subs = sel_goose_subscriptions(doc)     # {ied: [SelGooseSubscription]}

Every function also takes a plain path, and then parses one document for
itself. That is the convenience form, not the cheap one: on a real 22 MB
substation SCD `ET.parse` alone is 368 ms against about 100 ms of walking, so
a caller asking the same file three things should pass one `SclDocument`.

SEL extends SCL by two mechanisms, and both are standard SCL -- which is what
makes the split possible at all:

- **A value grammar inside a standard attribute.** `sAddr` is plain 61850-6;
  `db:52A|52B?0:1:2:3` is SEL's convention for what goes in it, and it is the
  ONLY bridge between the Relay Word name the GLE draws and the MMS item the
  relay answers to. The relay does not serve `sAddr` -- verified against a
  live SEL-451-5 R331, where `<LN>$DC$<DO>$d` answers `object-non-existent`
  -- so this can only come from a file. 178,406 DAIs in the reference station
  carry one.
- **`Private` elements.** `<Private type="SEL_GooseSubscription">` on an
  `<IED>`, holding the `pubRxStatus` that says which bit receives a
  subscription's health. Only 294 in the same file: SEL leans on the first
  mechanism, Siemens on the second.

Nothing here re-parses XML. Everything is read off `py61850` model nodes --
`attr.s_addr`, `attr.fc`, `ied.privates` -- which is the point of the split:
two readers of one file disagreeing about what is in it is the failure this
prevents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from py61850.scl import SclDocument, children_local

from sellib.scl.mms_tables import BitRule, da_rank, parse_saddr

_logger = logging.getLogger(__name__)

#: The `Private` type SEL Architect writes its subscription block under.
SEL_GOOSE_PRIVATE = "SEL_GooseSubscription"


def _document(source) -> SclDocument | None:
    """An `SclDocument` from a path or straight through from one.

    A path is parsed with `load()`, not `parse()`: the graceful constructor,
    because a path handed to one of these functions came from a user. A
    caller that wants an exception has already got one -- it passes the
    document it built with `parse()` itself.
    """
    if isinstance(source, SclDocument):
        return source
    return SclDocument.load(Path(source))


# -- sAddr: the Relay Word's name, inside the SCL --------------------------

@dataclass(frozen=True)
class ScdPoint:
    """Where a Relay Word bit lives in the 61850 model."""
    bit: str
    ld_inst: str
    ln: str          # prefix + lnClass + inst, como o MMS soletra
    do: str
    da: str          # 'stVal', or 'Oper.ctlVal' when it comes from an SDI
    # The functional constraint, resolved through the document's
    # `DataTypeTemplates`. It used to take a second walk of the whole file to
    # get (`sel_da_fcs`, now gone): the model resolves the type chain once and
    # every attribute carries its own FC, so the item is complete here.
    #
    # `None` only when the DA's type does not resolve -- a dangling `lnType`
    # or `DOType`. The point is still returned, because the bit HAS an
    # address and a caller may only want its name; what a caller must not do
    # is invent an FC to finish the item, because a guessed one produces an
    # MMS item the relay does not serve and the bit then disappears in
    # silence much further downstream.
    fc: str | None = None
    # How to take THIS bit out of the point's value, when the point carries
    # more than one (`sAddr="db:52A|52B?0:1:2:3"` on a DPS). `None` for a
    # plain address, which is the overwhelming majority -- 127,225 of the
    # corpus's 132,250. See `mms_tables.parse_saddr` and
    # `mms_tables.decode_bit`.
    rule: BitRule | None = None


def sel_short_addresses(source) -> dict[str, dict[str, ScdPoint]]:
    """`{ied_name: {BIT_NAME: ScdPoint}}` for every `sAddr="db:..."`.

    Takes an `SclDocument` or a path. Every IED in the document gets an entry,
    even an empty one: "this relay addresses no Relay Word bit" and "this
    relay is not in the file" are different answers.

    One name appears several times within an IED: the same bit shows up as
    `stVal` on the ST side and as `Oper.ctlVal` on the CO side, for instance.
    The one that stays is the one with the LOWEST `da_rank` -- boolean status
    first, DECORATED enumerated status next, a command last -- with the order
    the model yields breaking a tie.

    This cannot be left to an FC comparison further downstream: that one
    chooses between the FCs of ONE DA, and by then the status candidate would
    already have been thrown away. Measured on the reference SCD: under a
    plain first-wins, `LOCSTA` and `IPRST` (among 87 points of one IED)
    resolved to `CFG/LLN0.LocSta.Oper.ctlVal` -- so a viewer would read the
    command instead of the state.

    One `sAddr` can address TWO bits in a single point -- `db:52A|52B?0:1:2:3`
    on a `Pos$stVal`, whose Dbpos encodes both auxiliary contacts. Each name
    becomes its own `ScdPoint`, carrying the `rule` that says how to take its
    bit out of the value read. The grammar, and the invariant
    `len(alt) == 2**len(names)`, live in `mms_tables.parse_saddr`, and a shape
    that breaks it is discarded rather than guessed. Before that the key
    became the literal string `52A|52B?0:1:2:3` and the whole form vanished in
    silence: 55 of the 7,524 bits drawn across a substation's 25 relays, every
    one of them a breaker or disconnector position.
    """
    doc = _document(source)
    if doc is None:
        return {}
    out: dict[str, dict[str, ScdPoint]] = {}
    for name in doc.ied_names:
        ied = doc.ied(name)
        out[name] = {} if ied is None else _points_of(ied)
    return out


def _points_of(ied) -> dict[str, ScdPoint]:
    """The addressed bits of one resolved IED, best candidate per name.

    Walks the TYPE tree with the instance overlaid on it, which is what
    `py61850`'s model gives; the old reader walked the `DOI`/`SDI`/`DAI`
    elements directly. The two see the same addresses -- an `sAddr` only ever
    lives on a `DAI` -- and that was checked rather than assumed: 55,653 bits
    across the reference SEL station's 30 IEDs, identical name for name, with
    the same winning candidate for every one of them.
    """
    bits: dict[str, ScdPoint] = {}
    best: dict[str, tuple] = {}   # BIT -> rank of the candidate currently winning
    for ln in ied.logical_nodes():
        ld_inst = "" if ln.ldevice is None else ln.ldevice.inst
        for attr in ln.walk():
            if not attr.s_addr:
                continue
            spec = parse_saddr(attr.s_addr)
            if spec is None:
                continue
            # `path` is the descent from the data object down: ("Pos",
            # "Oper", "ctlVal"). The DO is its head, the DA everything under
            # it, spelled with '.' as SCL spells an SDI descent.
            do = attr.path[0]
            da = ".".join(attr.path[1:])
            rank = da_rank(da, decorated=spec.alternatives is not None)
            for i, bit in enumerate(spec.names):
                if bit in best and rank >= best[bit]:
                    continue
                best[bit] = rank
                bits[bit] = ScdPoint(bit=bit, ld_inst=ld_inst, ln=ln.name,
                                     do=do, da=da, fc=attr.fc,
                                     rule=spec.rule_for(i))
    return bits


# -- GOOSE health: the bit that says a publisher stopped arriving ----------

@dataclass(frozen=True)
class GooseRxStatus:
    """The bit that receives the HEALTH of one GOOSE subscription.

    SEL Architect declares it as `pubRxStatus` on the
    `<esel:GooseSubscription>` elements inside
    `<Private type="SEL_GooseSubscription">`, a direct child of the `<IED>`.
    The bit goes to 1 when the publisher stops arriving, so it is not a
    signal out of the dataset -- reading it as one is how a GOOSE-health bit
    ends up presented as a measurement nobody publishes.

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
    attribute entirely, so `sel_goose_subscriptions` never returned them;
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


def sel_goose_rx_status(source) -> dict[str, dict[str, GooseRxStatus]]:
    """`{ied_name: {BIT: GooseRxStatus}}`. Takes an `SclDocument` or a path.

    For each `<IED>`, every `<esel:GooseSubscription>` under
    `<Private type="SEL_GooseSubscription">` that declares a `pubRxStatus`,
    keyed by the bit that attribute names. An IED with no such declaration --
    and an SCD from a tool that writes no SEL private block at all -- is
    simply absent from the result.

    Read off `ied_headers`, not off a resolved IED: the private is a direct
    child of `<IED>` and answering this must not cost the instance tree. The
    reference station's 30 IEDs carry 178,406 DAIs between them and 30
    private blocks, and building the first to reach the second would spend
    the whole budget on the wrong half of the file.

    A bit is declared at most once per IED: it receives the health of one
    subscription. Should a file name the same bit twice, the first wins and
    the collision is logged, because two publishers feeding one health bit is
    a configuration error worth seeing rather than a merge to perform here.
    """
    doc = _document(source)
    if doc is None:
        return {}
    out: dict[str, dict[str, GooseRxStatus]] = {}
    for ied_name, header in doc.ied_headers.items():
        bits: dict[str, GooseRxStatus] = {}
        for private in header.privates.get(SEL_GOOSE_PRIVATE, ()):
            # DIRECT children of the private block. The block is SEL's own
            # and holds nothing else, but a descendant scan inside a
            # `<Private>` is the shape of bug that once let a DIGSI
            # cross-reference shadow 14 real IEDs -- see
            # `py61850.scl.document.SclDocument._ied_elements`.
            for sub in children_local(private, "GooseSubscription"):
                bit = (sub.get("pubRxStatus") or "").strip()
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
                    publisher_ied=(sub.get("iedName") or "").strip(),
                    src_ld_inst=(sub.get("ldInst") or "").strip(),
                    src_cb_name=(sub.get("cbName") or "").strip(),
                    dat_set=(sub.get("datSet") or None),
                )
        if bits:
            out[ied_name] = bits
    return out


@dataclass(frozen=True)
class SelGooseSubscription:
    """One GOOSE subscription of an IED, with SEL's health bit joined onto it.

    `py61850.scl.ExtRef` is the standard half and reports what the file says,
    unfiltered. This is the SEL projection of it: the subscriptions that name
    a publisher and a control block, deduplicated by the block they point at,
    each carrying the `pubRxStatus` bit SEL declared for it -- which lives in
    a `Private` block on the IED and not on the `ExtRef`, so the two halves
    have to be joined by somebody. Doing it here is the whole reason this
    type exists rather than callers being handed `ExtRef` directly.

    A subscription may resolve to no `GseAddress` in `Communication`; that is
    a mismatch to report, not a reason to drop it, so nothing here checks.
    """
    publisher_ied: str
    src_ld_inst: str
    src_cb_name: str
    desc: str | None = None         # `desc` do ExtRef (informativo)
    int_addr: str | None = None     # `intAddr` do ExtRef (informativo)
    # The bit that receives THIS subscription's health, when SEL Architect
    # declared one. `None` when the subscription maps no health bit, and
    # always `None` for an SCD with no SEL private block. See `GooseRxStatus`.
    rx_status_bit: str | None = None


def sel_goose_subscriptions(source) -> dict[str, list[SelGooseSubscription]]:
    """`{ied_name: [SelGooseSubscription, ...]}`. `SclDocument` or a path.

    For each `<IED>`, every `<ExtRef serviceType="GOOSE">` that has both
    `iedName` (the publisher) and `srcCBName` (the control block) filled in.
    An ExtRef missing either is ignored: those are empty templates, common in
    an SCD exported before every connection was made.

    Duplicate subscriptions to the same (publisher, ldInst, cbName) are
    collapsed, keeping the first -- the others are just further intAddrs of
    the same dataset.

    `serviceType` is filtered on here and NOT in `py61850`, which reports
    every ExtRef as the file spells it. That is the right division: the
    attribute is unreliable across exporting tools (see `GooseRxStatus`), so
    a general reader must not hide a different subset depending on who wrote
    the file, while this projection has to draw a line somewhere and draws it
    where SEL's tooling does. An IED contributing no subscription is absent
    from the result rather than present and empty.
    """
    doc = _document(source)
    if doc is None:
        return {}
    rx_by_ied = sel_goose_rx_status(doc)
    out: dict[str, list[SelGooseSubscription]] = {}
    for ied_name in doc.ied_names:
        ied = doc.ied(ied_name)
        if ied is None:
            continue
        rx_by_cb = {
            (s.publisher_ied, s.src_ld_inst, s.src_cb_name): s.bit
            for s in rx_by_ied.get(ied_name, {}).values()
        }
        seen: set[tuple[str, str, str]] = set()
        subs: list[SelGooseSubscription] = []
        for ext in ied.ext_refs():
            if (ext.service_type or "").upper() != "GOOSE":
                continue
            key = ext.source_key
            if key is None or key in seen:
                # `source_key` is None for a template ExtRef -- one naming no
                # publisher or no control block, which subscribes to nothing.
                continue
            seen.add(key)
            subs.append(SelGooseSubscription(
                publisher_ied=key[0],
                src_ld_inst=key[1],
                src_cb_name=key[2],
                desc=(ext.desc or None),
                int_addr=(ext.int_addr or None),
                rx_status_bit=rx_by_cb.get(key),
            ))
        if subs:
            out[ied_name] = subs
    return out
