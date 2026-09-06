"""The file formats an SEL protective relay project is made of.

Everything here reads (and, where it must, rewrites) files that AcSELerator
QuickSet produces. Nothing here talks to a relay, opens a socket, or knows
what a web request is.

    import sellib
    from sellib.rdb import process_upload
    from sellib.dnp_map import parse

    sellib.configure(user_data_dir="~/.pacct/data", cache_dir="/var/cache/rdb")

What is inside:

``sellib.rdb`` / ``sellib.rdb_cache``
    Extract an RDB (an OLE compound database) into a content-addressed cache,
    and list the relays, models and addresses it holds.
``sellib.settings``
    ``SET_*.TXT`` relay settings, tokenised faithfully.
``sellib.dnp_map``
    ``SET_D<n>.TXT``, the DNP3 point map, with a byte-for-byte round-trip
    contract: ``parse(b).serialize() == b``. These bytes go back into a
    protection relay, which is why that contract is the module's whole point.
``sellib.gle``
    QuickSet logic diagrams: parse, and render a page to SVG.
``sellib.selogic``
    SELOGIC control equations: parse, compare by equivalence rather than text,
    and normalise a relay's settings into a comparable model.
``sellib.models``
    Per-relay-model registries: block/bit conventions, and the Relay Word names
    a DNP map may legally use.
``sellib.scl``
    IEC 61850 SCL/SCD: IEDs, GOOSE control blocks and VLANs, ExtRef
    subscriptions, functional constraints, and SEL's ``sAddr`` addressing.
    Vendor-neutral apart from the ``db:`` grammar, which is SEL's convention
    living inside a standard format.
``sellib.match``
    Cross-match the relays in an RDB against the IEDs in an SCD.
``sellib.dnp_profile``
    Read an SEL DNP3 device profile bundle.

Writing a Compound File back out is `cfbwrite`, a separate library.
"""

from __future__ import annotations

from sellib._paths import (
    cache_dir,
    configure,
    data_dirs,
    user_data_dir,
    writable_data_dir,
)

__all__ = [
    "configure",
    "cache_dir",
    "data_dirs",
    "user_data_dir",
    "writable_data_dir",
]

__version__ = "2.2.0"
