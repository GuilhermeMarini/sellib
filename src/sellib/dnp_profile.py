"""SEL DNP3 Device Profile documents: the vendor's own point-name domains.

SEL publishes one of these per model/firmware as a zip (``dnp_r<rev><model>.zip``
or ``SEL-<model>_dnp.zip``) holding a ``dnpDP.xml`` plus the XSLT/XSD that render
it in a browser. The XML follows the DNP3 User Group's Device Profile schema, of
which two revisions appear in the corpus and both must be read:

    http://www.dnp3.org/DNP3/DeviceProfile/Jan2010
    http://www.dnp3.org/DNP3/DeviceProfile/April2016

What this module extracts is the **default point list**: the ``<name>`` of every
``binaryInput`` / ``binaryOutput`` / ``analogInput`` / ``analogOutput`` /
``counter`` the device ships with, mapped onto the SET_D block letters this
toolkit uses (BI / BO / AI / AO / CO).

What that list IS and IS NOT, because getting this wrong is the whole risk:

* For AO and CO it is the **complete domain**. Those fields take control-select
  macros and counter names the firmware defines, and nothing else. Measured
  across the real RDB corpus: 412 AO tokens and 2293 CO tokens, zero outside the
  profile.
* For BO it is the domain **once the ``close:open`` pair grammar is split** and
  unioned with the Relay Word (BO takes remote-bit names). Measured: 0 of 30377.
* For AI it is *most* of the domain. The profile omits math variables (MV01,
  AMV001) and some fault quantities (FIA, FIB); 47 distinct names, 4.4% of
  31664 tokens, fall outside it.
* For BI it is emphatically **NOT** the domain. A BI point can be mapped to any
  Relay Word bit, and the profile documents only the factory default map --
  28.6% of real BI values are outside it. The Relay Word list
  (``tools/wordbits_from_glv_cache.py``) remains the authority there.

So a profile is a source of per-kind name sets, not a replacement for the Relay
Word. ``sellib.models.wordbits`` merges both and decides per kind which it trusts.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from sellib.scl._xmlsafe import reject_dtd_in_bytes

# The schema namespaces seen in the corpus. Read the element's own namespace
# rather than matching either of these: a third revision should degrade to
# "parsed fine" and not to "no points found".
_KNOWN_NS = (
    "http://www.dnp3.org/DNP3/DeviceProfile/Jan2010",
    "http://www.dnp3.org/DNP3/DeviceProfile/April2016",
)

# Profile element -> the SET_D block letter the DNP map editor uses.
_ELEMENT_KIND = {
    "binaryInput": "BI",
    "binaryOutput": "BO",
    "analogInput": "AI",
    "analogOutput": "AO",
    "counter": "CO",
}

KINDS = ("BI", "BO", "AI", "AO", "CO")

# `deviceName` is prose, not a token: 'SEL-751', '"SEL-411L-0 Relay",
# "SEL-411L-1 Relay"', 'SEL-311C2, 311C3', 'SEL-487E-3, -4 Relay'. Pull every
# model-looking run out of it and let the caller widen to the base model.
_MODEL_RE = re.compile(r"SEL-([0-9]{3,4}[A-Z]{0,2}(?:-[0-9A-Z]{1,2})?)")

# A profile zip carries the XSD and the XSLT alongside the document; only the
# document is wanted, and it is the one XML that is neither.
_SKIP_XML = ("xsd", "xslt")

#: Ceiling on ONE decompressed member. The upload route caps the zip at 20 MB,
#: which is the COMPRESSED size -- a few hundred KB of zeros expand past any
#: memory a field laptop has. The largest real `dnpDP.xml` in the corpus is
#: under 2 MB, so this is an order of magnitude of slack.
MAX_MEMBER_BYTES = 64 * 1024 * 1024


class DnpProfileError(Exception):
    """The bytes handed over are not a readable DNP3 device profile."""


@dataclass
class DnpProfile:
    """One device profile: the models it covers and its default point names."""

    models: list[str] = field(default_factory=list)
    device_name: str = ""
    document_version: str = ""
    kinds: dict = field(default_factory=lambda: {k: set() for k in KINDS})
    source_name: str = ""

    def total(self) -> int:
        return sum(len(v) for v in self.kinds.values())


def model_keys(models: list[str]) -> list[str]:
    """Every key a profile should answer to, most specific first.

    ``['411L-0', '411L-1']`` -> ``['411L-0', '411L-1', '411L']``. The base model
    is included because a RELAYTYPE in an RDB is often written without the
    option digit, and a profile for ``-0`` describes the ``-1`` just as well for
    the purpose of naming points.
    """
    out: list[str] = []
    for m in models:
        for key in (m, m.split("-")[0]):
            if key and key not in out:
                out.append(key)
    return out


def _text(elem, ns: str, tag: str) -> str:
    found = elem.find(ns + tag)
    return (found.text or "").strip() if found is not None else ""


def _parse_xml(data: bytes, source_name: str = "") -> DnpProfile:
    try:
        # The profile comes out of a vendor zip the user chose.
        reject_dtd_in_bytes(data)
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise DnpProfileError(f"XML ilegível: {e}") from e

    ns = root.tag[: root.tag.find("}") + 1] if root.tag.startswith("{") else ""
    bare = root.tag[len(ns):]
    if bare != "DNP3DeviceProfileDocument":
        raise DnpProfileError(
            "o XML não é um DNP3 Device Profile "
            f"(elemento raiz {bare!r})."
        )

    prof = DnpProfile(source_name=source_name)
    prof.kinds = {k: set() for k in KINDS}

    name_el = root.find(".//" + ns + "deviceName")
    if name_el is not None:
        prof.device_name = _text(name_el, ns, "currentValue") or " ".join(
            (v.text or "").strip()
            for v in name_el.iter(ns + "value") if v.text
        )
    ver_el = root.find(".//" + ns + "documentVersionNumber")
    if ver_el is not None:
        prof.document_version = " ".join(
            (v.text or "").strip()
            for v in ver_el.iter(ns + "value") if v.text
        )

    prof.models = []
    for m in _MODEL_RE.findall(prof.device_name.upper()):
        if m not in prof.models:
            prof.models.append(m)

    for tag, kind in _ELEMENT_KIND.items():
        for point in root.findall(".//" + ns + tag):
            name = _text(point, ns, "name").upper()
            if name:
                prof.kinds[kind].add(name)

    if not prof.total():
        raise DnpProfileError(
            "o perfil não declara nenhum ponto DNP (lista de pontos vazia)."
        )
    if not prof.models:
        raise DnpProfileError(
            "não foi possível deduzir o modelo do relé a partir de "
            f"deviceName={prof.device_name!r}."
        )
    return prof


def parse_zip(data: bytes, source_name: str = "") -> DnpProfile:
    """Read the ``dnpDP.xml`` out of a profile zip.

    SEL names the document ``dnpDP.xml`` in most bundles and ``dnp_<model>.xml``
    in others, and always ships the XSD/XSLT next to it -- hence picking by
    exclusion rather than by name.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise DnpProfileError(f"zip inválido: {e}") from e
    with zf:
        candidates = [
            n for n in zf.namelist()
            if n.lower().endswith(".xml")
            and not any(s in n.lower() for s in _SKIP_XML)
        ]
        if not candidates:
            raise DnpProfileError(
                "o zip não contém o XML do perfil (dnpDP.xml)."
            )
        # Deterministic pick, and prefer the canonical filename when present.
        candidates.sort(key=lambda n: (0 if "dnpdp" in n.lower() else 1, n))
        last: DnpProfileError | None = None
        for name in candidates:
            try:
                info = zf.getinfo(name)
            except KeyError:                     # pragma: no cover - namelist
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                last = DnpProfileError(
                    f"{name} descompacta para {info.file_size} bytes, acima "
                    f"do limite de {MAX_MEMBER_BYTES}.")
                continue
            try:
                return _parse_xml(zf.read(name), source_name or name)
            except DnpProfileError as e:
                last = e
        raise last or DnpProfileError("nenhum XML utilizável no zip.")


def parse(data: bytes, source_name: str = "") -> DnpProfile:
    """Parse a profile from either a zip bundle or a bare XML document."""
    if data[:2] == b"PK":
        return parse_zip(data, source_name)
    return _parse_xml(data, source_name)


def parse_path(path: Path) -> DnpProfile:
    return parse(Path(path).read_bytes(), Path(path).name)
