"""SEL's half of an IEC 61850 SCL file, and the bit -> MMS item tables.

The standard half left for `py61850.scl`, which reads an SCL file as a general
IEC 61850-6 object model -- the document, the `DataTypeTemplates` pool, the
`Communication` section and a per-IED instance tree. This subpackage holds only
what is SEL's and could not honestly go in a vendor-neutral library:

``read``
    The `db:` grammar SEL writes inside the standard `sAddr` attribute, and
    the `pubRxStatus` health bit it declares in a `Private` block.
``mms_tables``
    The shipped `data/mms_map/*.json` registry, and the name-only heuristics
    that stand in for a type when the live path has no file behind it.

Both read off `py61850` model nodes and neither parses XML. The seam is that
`py61850` exposes the standard attribute and takes no view on the vendor
grammar inside its value, and exposes every `Private` element without knowing
what any of them mean.
"""
