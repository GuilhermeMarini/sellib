"""
Compare two SELOGIC values, with four possible verdicts:

  1. EQUAL                       -- the same string once whitespace is
                                    stripped, comments included
  2. EQUAL_LOGIC_DIFF_COMMENT    -- identical body, different comments (4xx
                                    and 7xx only; the 3xx have no `#`)
  3. EQUIVALENT                  -- the same boolean function written
                                    differently (caught by the canonicalised
                                    AST and/or a truth table, up to 16 atoms)
  4. DIFFERENT                   -- they evaluate differently in at least one
                                    state, OR there are more than 16 atoms and
                                    no canonical match (noted as "not
                                    exhaustively verified")

For values that are not boolean:
  - kind="number"   : parsed as a float and compared with a tolerance
  - kind="enum"     : trimmed string equality (case sensitive: 'Y' != 'y')
  - kind="string"   : trimmed string equality

The API takes the RAW values straight from the SET_*.TXT parser -- an inline
`# ...` comment is still in the value, and stripping it is this module's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sellib.selogic import parser as sp

Verdict = Literal[
    "EQUAL",
    "EQUAL_LOGIC_DIFF_COMMENT",
    "EQUIVALENT",
    "DIFFERENT",
]


Kind = Literal["logic", "number", "enum", "string", "set_list"]


@dataclass(frozen=True)
class CompareResult:
    verdict: Verdict
    note: str | None = None


# Where the truth-table fallback gives up. Beyond this the binary explosion
# (2^N) gets expensive: 2^16 is 65k rows, 2^20 is a million. 16 is the
# practical ceiling; real SELOGIC equations rarely reach ~10 distinct atoms.
_MAX_TRUTH_ATOMS = 16


def _split_logic_and_comment(raw: str) -> tuple[str, str]:
    """Returns (body, comment), both trimmed. The comment comes back without its
    leading `#`."""
    h = raw.find("#")
    if h < 0:
        return raw.strip(), ""
    return raw[:h].strip(), raw[h + 1 :].strip()


def _normalize_whitespace(s: str) -> str:
    return " ".join(s.split())


def _compare_logic(
    a_body: str,
    b_body: str,
    dialect: sp.Dialect,
) -> CompareResult:
    """Compare two bodies with their comments already removed. Runs the
    canonical-form then truth-table cascade. Returns EQUIVALENT or DIFFERENT,
    never EQUAL -- that verdict is decided by the caller."""
    try:
        ast_a = sp.parse(a_body, dialect)
        ast_b = sp.parse(b_body, dialect)
    except sp.ParseError:
        # Nao parseou como booleano -- pode ser equacao matematica ou
        # sintaxe estranha. Compara como string normalizada.
        if _normalize_whitespace(a_body) == _normalize_whitespace(b_body):
            return CompareResult("EQUIVALENT", note="texto coincidente (sem parse)")
        return CompareResult("DIFFERENT", note="nao parseavel como booleano")

    can_a = sp.canonicalize(ast_a)
    can_b = sp.canonicalize(ast_b)
    if sp.node_repr(can_a) == sp.node_repr(can_b):
        return CompareResult("EQUIVALENT")

    # Fallback de tabela verdade
    union = sp.atoms(can_a) | sp.atoms(can_b)
    if len(union) > _MAX_TRUTH_ATOMS:
        return CompareResult(
            "DIFFERENT",
            note=f"nao verificado exaustivamente (>{_MAX_TRUTH_ATOMS} atomos)",
        )

    names = sorted(union)
    n = len(names)
    for mask in range(1 << n):
        env = {names[k]: bool((mask >> k) & 1) for k in range(n)}
        if sp.evaluate(can_a, env) != sp.evaluate(can_b, env):
            return CompareResult("DIFFERENT")

    return CompareResult("EQUIVALENT")


def compare_logic(
    a_raw: str,
    b_raw: str,
    dialect: sp.Dialect,
) -> CompareResult:
    """Compare two SELOGIC equations of the same dialect. Four verdicts."""
    a_body, a_comment = _split_logic_and_comment(a_raw)
    b_body, b_comment = _split_logic_and_comment(b_raw)

    a_norm = _normalize_whitespace(a_body)
    b_norm = _normalize_whitespace(b_body)

    if a_norm == b_norm:
        if a_comment.strip() == b_comment.strip():
            return CompareResult("EQUAL")
        return CompareResult("EQUAL_LOGIC_DIFF_COMMENT")

    return _compare_logic(a_body, b_body, dialect)


def _try_float(s: str) -> float | None:
    try:
        return float(s.strip())
    except (ValueError, AttributeError):
        return None


def compare_number(
    a_raw: str,
    b_raw: str,
    *,
    rel_tol: float = 1e-6,
    abs_tol: float = 1e-6,
) -> CompareResult:
    """Compare two numbers, tolerating formatting (0 vs 0.000000) and float noise."""
    a_str = a_raw.strip()
    b_str = b_raw.strip()
    if a_str == b_str:
        return CompareResult("EQUAL")
    fa = _try_float(a_str)
    fb = _try_float(b_str)
    if fa is None or fb is None:
        # At least one is not a number -- fall back to comparing strings.
        return CompareResult("DIFFERENT")
    diff = abs(fa - fb)
    if diff <= max(abs_tol, rel_tol * max(abs(fa), abs(fb))):
        return CompareResult("EQUIVALENT", note="numericamente iguais")
    return CompareResult("DIFFERENT")


def compare_enum(a_raw: str, b_raw: str) -> CompareResult:
    if a_raw.strip() == b_raw.strip():
        return CompareResult("EQUAL")
    return CompareResult("DIFFERENT")


def compare_string(a_raw: str, b_raw: str) -> CompareResult:
    return compare_enum(a_raw, b_raw)


def _parse_set_list(raw: str) -> set[str]:
    """QuickSet writes SER lists as plain CSV; ALIAS lists are space-separated."""
    # Aceita virgulas, espacos, tabs e ponto-e-virgula como delimitadores.
    import re
    return {t for t in re.split(r"[,\s;]+", raw.strip()) if t}


def compare_set_list(a_raw: str, b_raw: str) -> CompareResult:
    """Compare two lists as sets: order and duplicates are ignored.

    Used for the SER (Sequence of Events Recorder) lists and their like, where
    the relay records a *set* of word bits and the position in the file
    carries no meaning.

    Verdicts:
      - EQUAL              : the raw text is identical
      - EQUIVALENT         : same set, text reordered
      - DIFFERENT          : different sets (the note names what is extra and
                             what is missing)
    """
    a_text = " ".join(a_raw.split())
    b_text = " ".join(b_raw.split())
    if a_text == b_text:
        return CompareResult("EQUAL")
    a_set = _parse_set_list(a_raw)
    b_set = _parse_set_list(b_raw)
    if a_set == b_set:
        return CompareResult("EQUIVALENT", note="mesmo conjunto, ordem diferente")
    only_a = sorted(a_set - b_set)
    only_b = sorted(b_set - a_set)
    parts: list[str] = []
    if only_a:
        sample = only_a if len(only_a) <= 6 else only_a[:6] + [f"+{len(only_a)-6}"]
        parts.append(f"sobra em A: {', '.join(sample)}")
    if only_b:
        sample = only_b if len(only_b) <= 6 else only_b[:6] + [f"+{len(only_b)-6}"]
        parts.append(f"sobra em B: {', '.join(sample)}")
    return CompareResult("DIFFERENT", note=" | ".join(parts) or None)


def compare(
    a_raw: str,
    b_raw: str,
    *,
    kind: Kind,
    dialect: sp.Dialect = "keyword",
) -> CompareResult:
    """The main dispatcher. `kind` picks the path (logic/number/enum/string);
    `dialect` only matters for `kind="logic"`."""
    if kind == "logic":
        return compare_logic(a_raw, b_raw, dialect)
    if kind == "number":
        return compare_number(a_raw, b_raw)
    if kind == "enum":
        return compare_enum(a_raw, b_raw)
    if kind == "string":
        return compare_string(a_raw, b_raw)
    if kind == "set_list":
        return compare_set_list(a_raw, b_raw)
    raise ValueError(f"kind desconhecido: {kind!r}")


def compare_n(
    values: list[str],
    *,
    kind: Kind,
    dialect: sp.Dialect = "keyword",
) -> tuple[Verdict, str | None]:
    """Compare N >= 2 values against `values[0]`. Severity runs
    EQUAL < EQUAL_LOGIC_DIFF_COMMENT < EQUIVALENT < DIFFERENT, and the worst
    of those N-1 comparisons is the verdict.

    Comparing against a PIVOT rather than across every pair is sound, and not
    a shortcut: EQUAL and EQUIVALENT are both transitive, so `a == b` and
    `a == c` give `b == c` for free, and the worst verdict cannot hide in a
    pair the pivot did not touch. Do not "fix" this into O(N^2) -- it would
    cost the truth table N^2/2 runs for the same answer.

    Per-pair notes are aggregated, first non-empty winning -- enough for the
    UI to label the most severe disagreement among the relays.
    """
    if len(values) < 2:
        return ("EQUAL", None)

    severity: dict[Verdict, int] = {
        "EQUAL": 0,
        "EQUAL_LOGIC_DIFF_COMMENT": 1,
        "EQUIVALENT": 2,
        "DIFFERENT": 3,
    }
    worst: Verdict = "EQUAL"
    note: str | None = None
    a = values[0]
    for b in values[1:]:
        r = compare(a, b, kind=kind, dialect=dialect)
        if severity[r.verdict] > severity[worst]:
            worst = r.verdict
            note = r.note
        elif severity[r.verdict] == severity[worst] and note is None:
            note = r.note
    return (worst, note)
