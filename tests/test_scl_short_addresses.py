"""`sAddr="db:NAME"`: the only bridge from a Relay Word bit to an MMS item.

The relay does not serve `sAddr` -- verified against a live SEL-451-5 R331,
where `<LN>$DC$<DO>$d` answers `object-non-existent` -- so a bit's 61850
address can only come from a file. Getting it wrong is not a missing feature:
it is a commissioning screen showing the wrong point, or a breaker painted
closed while it is open.

These assertions used to live only in `pac-ct`, one repository downstream of
the code they pin. They are here now because the walk was rewritten onto
`py61850`'s object model -- it reads `attr.s_addr` and `attr.fc` off resolved
type-and-instance nodes instead of descending the `DOI`/`SDI`/`DAI` elements
-- and a rewrite whose only test is in another repository is a rewrite nobody
can check before pushing.

The rewrite was checked against the old walk over the reference SEL station
before it landed: 55,653 bits across 30 IEDs, identical name for name, with
the same winning candidate for every one -- and every winner resolving an FC,
which the old walk needed a second pass over the whole file to answer.
"""
from __future__ import annotations

from pathlib import Path

from py61850.scl import SclDocument

from sellib.scl.read import ScdPoint, sel_short_addresses

FIXTURE = Path(__file__).parent / "fixtures" / "saddr_min.scd"


def _bits() -> dict[str, ScdPoint]:
    return sel_short_addresses(FIXTURE)["REL_A"]


class TestWhatIsAddressed:
    def test_every_well_formed_db_address_becomes_a_bit(self):
        assert set(_bits()) == {
            "LOC", "PLT01", "PLT02", "SV06", "RB01", "LOCSTA", "52A", "52B",
        }

    def test_a_name_is_upper_cased(self):
        """`db:sv06` and `db:SV06` are one bit. The Relay Word is upper case
        and the GLE draws it that way; the SCD does not always agree."""
        assert "SV06" in _bits() and "sv06" not in _bits()

    def test_an_sdi_descent_becomes_a_dotted_da(self):
        """`SDI Oper` + `DAI ctlVal` is the `Oper.ctlVal` MMS leaf. RB01 has
        no status DA at all -- a remote bit is written, not read -- so a
        reader that dropped commands would hide it entirely."""
        rb01 = _bits()["RB01"]
        assert (rb01.ln, rb01.do, rb01.da) == ("RBGGIO1", "SPCSO01",
                                               "Oper.ctlVal")

    def test_a_malformed_decoration_is_refused_rather_than_guessed(self):
        """`db:BADFORM?0:1:2` gives one name three alternatives, and the
        invariant is `len(alternatives) == 2**len(names)` -- 5,025 of 5,025
        across the corpus. A shape nobody has seen must not become a reading:
        the guess here is a breaker painted closed while it is open."""
        assert "BADFORM" not in _bits()

    def test_an_ied_that_addresses_nothing_is_present_and_empty(self, tmp_path):
        """"this relay maps no Relay Word bit" and "this relay is not in the
        file" are different answers, and a caller acts differently on each."""
        scd = tmp_path / "bare.scd"
        scd.write_text(
            '<?xml version="1.0"?>'
            '<SCL xmlns="http://www.iec.ch/61850/2003/SCL">'
            '<IED name="REL_B"/></SCL>', encoding="utf-8")
        assert sel_short_addresses(scd) == {"REL_B": {}}


class TestTheFunctionalConstraintComesWithThePoint:
    """It used to take a second walk of the whole file, joined on a four-part
    key (`sel_da_fcs`). The model resolves the type chain once, so the point
    arrives complete."""

    def test_a_status_point_is_st(self):
        assert _bits()["PLT01"].fc == "ST"

    def test_a_command_inherits_the_fc_of_the_root_da(self):
        """`Oper` is `CO` on the DOType and `ctlVal` descends inside it. IEC
        61850 puts the FC on the root attribute; everything under it comes
        along, which is why the item is `RBGGIO1$CO$SPCSO01$Oper$ctlVal`."""
        assert _bits()["RB01"].fc == "CO"

    def test_the_fc_is_read_and_not_assumed(self):
        """All 2,030 decorated addresses across the corpus's 146 ICDs are
        `ST`. Resolving anyway is what makes a future ICD that disagrees fail
        loudly instead of writing an item the relay does not serve."""
        assert _bits()["52A"].fc == "ST"


class TestWhichCandidateWinsWhenABitHasSeveral:
    def test_a_status_beats_the_command_that_sets_it(self):
        """LOCSTA appears as `SPCSO02/Oper/ctlVal` (a command) and, later in
        the same LN's type, flat as `Ind04/stVal`. Under a plain first-wins
        the command won: measured on the reference SCD, `LOCSTA`, `IPRST` and
        85 other bits of one IED resolved to an `Oper.ctlVal`, so a viewer
        polled what someone last wrote instead of what the relay sees."""
        locsta = _bits()["LOCSTA"]
        assert (locsta.do, locsta.da) == ("Ind04", "stVal")
        assert locsta.fc == "ST"

    def test_a_plain_boolean_beats_a_decorated_enumeration_of_the_same_name(self):
        """`LOC` has a plain `db:LOC` on `LLN0.Loc.stVal` and a decorated
        `db:LOC?3:1` on a `Health.stVal`. Both are `stVal`, so without a tier
        of its own the tie would fall back to whatever order the walk
        happened to produce. Measured on one IED of the substation SCD: 10 of
        its 33 decorated names also carry a plain address."""
        loc = _bits()["LOC"]
        assert (loc.ln, loc.do) == ("LLN0", "Loc")
        assert loc.rule is None


class TestOnePointCanCarryTwoBits:
    """`db:52A|52B?0:1:2:3` on a `Pos$stVal`, whose Dbpos encodes both
    auxiliary contacts. Before the grammar was parsed the key became the
    literal string `52A|52B?0:1:2:3` and the whole form vanished in silence:
    55 of the 7,524 bits drawn across a substation's 25 relays, every one of
    them a breaker or disconnector position."""

    def test_both_names_become_their_own_point(self):
        a, b = _bits()["52A"], _bits()["52B"]
        assert a.do == b.do == "Pos"
        assert a.ln == b.ln == "BKR1CSWI1"

    def test_each_carries_the_rule_that_extracts_its_own_bit(self):
        """First name as the MOST SIGNIFICANT bit: (0,0)->0, (0,1)->1,
        (1,0)->2, (1,1)->3, which is exactly IEC 61850's Dbpos."""
        assert _bits()["52A"].rule.index == 0
        assert _bits()["52B"].rule.index == 1
        for bit in ("52A", "52B"):
            assert _bits()[bit].rule.alternatives == (0, 1, 2, 3)
            assert _bits()[bit].rule.nbits == 2

    def test_a_plain_address_carries_no_rule(self):
        """127,225 of the corpus's 132,250 `db:` addresses are plain. A rule
        on one of those would be a decode step applied to a boolean."""
        assert _bits()["PLT01"].rule is None


class TestTheSourceCanBeADocumentOrAPath:
    def test_a_document_answers_the_same_as_a_path(self):
        """A caller reading three things out of one file passes one
        `SclDocument`: on a real 22 MB SCD the parse alone is 368 ms against
        about 100 ms of walking."""
        assert sel_short_addresses(SclDocument.parse(FIXTURE)) == \
            sel_short_addresses(FIXTURE)

    def test_an_unreadable_file_is_an_empty_map_and_not_an_exception(
            self, tmp_path):
        bad = tmp_path / "broken.scd"
        bad.write_text("<SCL>", encoding="utf-8")
        assert sel_short_addresses(bad) == {}

    def test_a_missing_file_is_an_empty_map_too(self, tmp_path):
        assert sel_short_addresses(tmp_path / "nao_existe.scd") == {}


class TestWhatTheTemplatesDoNotDeclareIsNotInvented:
    """The boundary the model draws, stated deliberately rather than
    discovered later.

    An attribute reaches this reader through its DOType, so a `DAI` naming
    one the templates do not declare addresses nothing. That is the same
    refusal `parse_saddr` makes on a malformed decoration and the generator
    makes on an unresolvable FC: a point invented here becomes an MMS item
    the relay does not serve, and the bit then disappears in silence much
    further downstream. Measured over the reference SEL station, it costs
    nothing -- 0 of 55,653 bits -- because a real ICD declares its types.
    """

    def _scd(self, tmp_path, dotype_body):
        text = (
            '<?xml version="1.0"?>'
            '<SCL xmlns="http://www.iec.ch/61850/2003/SCL"><IED name="REL_C">'
            '<AccessPoint name="S1"><Server><LDevice inst="ANN">'
            '<LN0 lnType="T_LLN0" lnClass="LLN0" inst="">'
            '<DOI name="Loc"><DAI name="stVal" sAddr="db:LOC"/></DOI>'
            '</LN0></LDevice></Server></AccessPoint></IED>'
            '<DataTypeTemplates>'
            '<LNodeType id="T_LLN0" lnClass="LLN0">'
            '<DO name="Loc" type="T_SPS"/></LNodeType>'
            f'<DOType id="T_SPS" cdc="SPS">{dotype_body}</DOType>'
            '</DataTypeTemplates></SCL>')
        path = tmp_path / "partial.scd"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_declared_attribute_is_addressed(self, tmp_path):
        scd = self._scd(tmp_path, '<DA name="stVal" fc="ST" bType="BOOLEAN"/>')
        assert sel_short_addresses(scd)["REL_C"]["LOC"].fc == "ST"

    def test_an_undeclared_one_addresses_nothing(self, tmp_path):
        scd = self._scd(tmp_path, '<DA name="q" fc="ST" bType="Quality"/>')
        assert sel_short_addresses(scd) == {"REL_C": {}}
