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
"""
from __future__ import annotations

from pathlib import Path

from sellib.scl.read import (
    GooseRxStatus,
    ScdDocument,
    extract_goose_rx_status_by_ied,
)

FIXTURE = Path(__file__).parent / "fixtures" / "goose_rx_status.scd"


def _doc() -> ScdDocument:
    doc = ScdDocument.load(FIXTURE)
    assert doc is not None
    return doc


class TestGooseRxStatus:
    def test_the_health_bit_is_read_with_the_control_block_it_watches(self):
        by_ied = _doc().goose_rx_status_by_ied()
        assert by_ied["REL_SUB"] == {
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
        by_ied = _doc().goose_rx_status_by_ied()
        assert set(by_ied["REL_PUB1"]) == {"RB11"}

    def test_a_subscription_without_pubRxStatus_contributes_nothing(self):
        # REL_PUB2's subscription is real; its health simply is not mapped.
        bits = _doc().goose_rx_status_by_ied()["REL_SUB"]
        assert not any(s.publisher_ied == "REL_PUB2" for s in bits.values())

    def test_an_ied_with_no_private_block_is_absent_rather_than_empty(self):
        by_ied = _doc().goose_rx_status_by_ied()
        assert "REL_PLAIN" not in by_ied

    def test_the_module_level_twin_answers_the_same(self):
        assert extract_goose_rx_status_by_ied(FIXTURE) == _doc().goose_rx_status_by_ied()

    def test_an_unreadable_scd_is_an_empty_map_and_not_an_exception(self, tmp_path):
        bad = tmp_path / "broken.scd"
        bad.write_text("<SCL>", encoding="utf-8")
        assert extract_goose_rx_status_by_ied(bad) == {}


class TestSubscriptionCarriesItsHealthBit:
    def test_the_subscription_names_its_own_health_bit(self):
        subs = {
            (s.publisher_ied, s.src_cb_name): s
            for s in _doc().goose_subscriptions_by_ied()["REL_SUB"]
        }
        assert subs[("REL_PUB1", "GoSB00")].rx_status_bit == "VB051"

    def test_a_subscription_with_no_health_bit_says_none(self):
        subs = {
            (s.publisher_ied, s.src_cb_name): s
            for s in _doc().goose_subscriptions_by_ied()["REL_SUB"]
        }
        assert subs[("REL_PUB2", "GoSB01")].rx_status_bit is None

    def test_the_health_extref_does_not_invent_a_second_subscription(self):
        # VB051's ExtRef names the same control block as VB011's, so the two
        # collapse into one subscription -- which is what keeps the VLAN
        # mapper's RX count honest.
        subs = _doc().goose_subscriptions_by_ied()["REL_SUB"]
        assert len(subs) == 2

    def test_a_health_bit_is_named_even_when_its_extref_is_not_a_subscription(self):
        """`serviceType` does not discriminate, and the two sample files
        disagree on it: substation_demo omits it on all 202 health ExtRefs
        (so they never reach `goose_subscriptions_by_ied`), IEC station 1
        writes `GOOSE` on its one (so it does). REL_PUB1 carries the first
        shape -- the health bit must still be named."""
        doc = _doc()
        assert "REL_PUB1" not in doc.goose_subscriptions_by_ied()
        assert doc.goose_rx_status_by_ied()["REL_PUB1"]["RB11"].src_cb_name == "GPub01"


class TestTheExtRefShapeAgreesWithTheDeclaration:
    """The invariant behind the choice not to guess. If a future SCD breaks
    it, this fails and says so instead of a reader quietly mislabelling a
    signal as a health bit."""

    def test_every_declared_health_bit_has_an_extref_with_no_do_or_da(self):
        from sellib.scl.read import _iter_local

        # Through `ScdDocument`, so this walk gets the same `<!DOCTYPE>`
        # refusal every other read of an SCD gets.
        doc = _doc()
        root = doc.root
        by_ied = doc.goose_rx_status_by_ied()
        checked = 0
        for ied in _iter_local(root, "IED"):
            declared = by_ied.get(ied.attrib.get("name") or "", {})
            shaped = {
                (e.attrib.get("intAddr") or "").strip()
                for e in _iter_local(ied, "ExtRef")
                if (e.attrib.get("iedName") or "").strip()
                and (e.attrib.get("srcCBName") or "").strip()
                and not (e.attrib.get("doName") or "").strip()
                and not (e.attrib.get("daName") or "").strip()
            }
            assert shaped == set(declared)
            checked += len(declared)
        assert checked == 2
