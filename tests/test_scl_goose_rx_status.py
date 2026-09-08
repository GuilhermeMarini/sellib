"""`pubRxStatus`: the bit that receives a GOOSE subscription's health.

SEL Architect writes it on `<esel:GooseSubscription>` inside
`<Private type="SEL_GooseSubscription">`. The bit it names is not a signal
out of the dataset -- it goes to 1 when the publisher stops arriving -- and
that is exactly what an SCD reader must not confuse with a data subscription.

The `<ExtRef>` twin of a health bit is recognisable on its own: it carries a
publisher and a control block and NO `doName`/`daName`, because there is no
data attribute to point at. Measured across the two sample substations of
`pac-ct` -- 60 IEDs, 203 health bits -- the shape and `pubRxStatus` agree
203/203 in both directions. It is asserted here as an invariant and is
deliberately NOT a fallback in the reader: `pubRxStatus` is the declared
fact, and inferring a GOOSE-health bit from a shape is the same class of
guess as reading a breaker's position out of an undecorated Dbpos.

The `Private` block is read off `py61850`'s model -- `IedHeader.privates` --
and the `ExtRef`s off `Ied.ext_refs()`. Neither half re-parses the XML, which
is what stops the two readers of one file ever disagreeing about it.
"""
from __future__ import annotations

from pathlib import Path

from py61850.scl import SclDocument

from sellib.scl.read import (
    GooseRxStatus,
    sel_goose_rx_status,
    sel_goose_subscriptions,
)

FIXTURE = Path(__file__).parent / "fixtures" / "goose_rx_status.scd"


def _doc() -> SclDocument:
    doc = SclDocument.load(FIXTURE)
    assert doc is not None
    return doc


class TestGooseRxStatus:
    def test_the_health_bit_is_read_with_the_control_block_it_watches(self):
        assert sel_goose_rx_status(_doc())["REL_SUB"] == {
            "VB051": GooseRxStatus(
                bit="VB051",
                publisher_ied="REL_PUB1",
                src_ld_inst="CFG",
                src_cb_name="GoSB00",
                dat_set="GOPB_138",
            ),
        }

    def test_a_health_bit_is_not_always_a_virtual_bit(self):
        # 5 of the 202 in the corpus sample are remote bits. The reader names
        # whatever `pubRxStatus` names; filtering to VBnnn is a caller's job.
        assert set(sel_goose_rx_status(_doc())["REL_PUB1"]) == {"RB11"}

    def test_a_subscription_without_pubRxStatus_contributes_nothing(self):
        # REL_PUB2's subscription is real; its health simply is not mapped.
        bits = sel_goose_rx_status(_doc())["REL_SUB"]
        assert not any(s.publisher_ied == "REL_PUB2" for s in bits.values())

    def test_an_ied_with_no_private_block_is_absent_rather_than_empty(self):
        assert "REL_PLAIN" not in sel_goose_rx_status(_doc())

    def test_a_path_answers_the_same_as_a_document(self):
        """The convenience form parses one document for itself. It must not
        be a second implementation of anything."""
        assert sel_goose_rx_status(FIXTURE) == sel_goose_rx_status(_doc())

    def test_an_unreadable_scd_is_an_empty_map_and_not_an_exception(self, tmp_path):
        bad = tmp_path / "broken.scd"
        bad.write_text("<SCL>", encoding="utf-8")
        assert sel_goose_rx_status(bad) == {}
        assert sel_goose_subscriptions(bad) == {}

    def test_the_health_bit_is_read_without_building_the_instance_tree(self):
        """It comes off `ied_headers`, which is the cheap half of the model.

        The private is a direct child of `<IED>`, so answering this must not
        cost a walk of 178,406 DAIs -- which is what resolving an IED does on
        the reference station. A document that refuses to resolve one proves
        it without measuring anything.
        """
        class NoInstanceTree(SclDocument):
            def ied(self, name):
                raise AssertionError(
                    "resolved an IED just to read a Private block")

        base = _doc()
        doc = NoInstanceTree(base.root, base.path, base.namespaces)
        assert sel_goose_rx_status(doc)["REL_SUB"]


class TestSubscriptionCarriesItsHealthBit:
    def test_the_subscription_names_its_own_health_bit(self):
        subs = {
            (s.publisher_ied, s.src_cb_name): s
            for s in sel_goose_subscriptions(_doc())["REL_SUB"]
        }
        assert subs[("REL_PUB1", "GoSB00")].rx_status_bit == "VB051"

    def test_a_subscription_with_no_health_bit_says_none(self):
        subs = {
            (s.publisher_ied, s.src_cb_name): s
            for s in sel_goose_subscriptions(_doc())["REL_SUB"]
        }
        assert subs[("REL_PUB2", "GoSB01")].rx_status_bit is None

    def test_the_health_extref_does_not_invent_a_second_subscription(self):
        # VB051's ExtRef names the same control block as VB011's, so the two
        # collapse into one subscription -- which is what keeps the VLAN
        # mapper's RX count honest.
        assert len(sel_goose_subscriptions(_doc())["REL_SUB"]) == 2

    def test_a_template_extref_is_not_a_subscription(self):
        """`VB099` names no publisher and no control block: an input left
        unbound by an SCD exported before every connection was made.
        `py61850` reports it, because which inputs are still unbound is a
        real question; this projection is about subscriptions and drops it."""
        subs = sel_goose_subscriptions(_doc())["REL_SUB"]
        assert all(s.publisher_ied and s.src_cb_name for s in subs)

    def test_a_health_bit_is_named_even_when_its_extref_is_not_a_subscription(self):
        """`serviceType` does not discriminate, and the two sample files
        disagree on it: substation_demo omits it on all 202 health ExtRefs
        (so they never reach `sel_goose_subscriptions`), IEC station 1
        writes `GOOSE` on its one (so it does). REL_PUB1 carries the first
        shape -- the health bit must still be named."""
        doc = _doc()
        assert "REL_PUB1" not in sel_goose_subscriptions(doc)
        assert sel_goose_rx_status(doc)["REL_PUB1"]["RB11"].src_cb_name == "GPub01"


class TestTheExtRefShapeAgreesWithTheDeclaration:
    """The invariant behind the choice not to guess. If a future SCD breaks
    it, this fails and says so instead of a reader quietly mislabelling a
    signal as a health bit."""

    def test_every_declared_health_bit_has_an_extref_with_no_do_or_da(self):
        # Through the model, so this walk sees exactly what the reader sees
        # -- including the `<!DOCTYPE>` refusal every read of an SCL file
        # gets, and the ExtRefs of a gateway LN declared outside a Server.
        doc = _doc()
        by_ied = sel_goose_rx_status(doc)
        checked = 0
        for name in doc.ied_names:
            declared = by_ied.get(name, {})
            shaped = {
                (ref.int_addr or "").strip()
                for ref in doc.ied(name).ext_refs()
                if ref.is_bound
                and not (ref.do_name or "").strip()
                and not (ref.da_name or "").strip()
            }
            assert shaped == set(declared)
            checked += len(declared)
        assert checked == 2
