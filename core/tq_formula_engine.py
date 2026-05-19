"""
core/tq_formula_engine.py
==========================
Pure-Python formula implementations for every scoring pattern found in
Indian government RFP evaluation tables.

No LLM involved in any formula. LLM is only used UPSTREAM to extract the
single fact (the number/value) that the formula then processes.

Formula types
-------------
STEP      Turnover-style: base value gets N marks; +M marks per additional X Cr
BAND      Professionals/manpower: ordered threshold bands
PER_UNIT  Projects: N marks per qualifying project, capped at max
QUAL      Qualifications: structured binary evidence check (weighted)
BINARY    Yes/No: registered / certified / methodology present
LLM       Fallback for complex criteria (methodology, approach quality)

Each function returns (score: float, steps_description: str).
score is already clamped to [0, max_marks].
"""

from __future__ import annotations

import re
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Value parsing helpers
# ──────────────────────────────────────────────────────────────────────────────

def _nums(text: str) -> list[float]:
    """Extract all numbers (including decimals) from a string."""
    return [float(m.replace(",", "")) for m in
            re.findall(r"[\d,]+(?:\.\d+)?", (text or "").replace(",", ""))]


def _first_num(text: str) -> Optional[float]:
    ns = _nums(text)
    return ns[0] if ns else None


# ──────────────────────────────────────────────────────────────────────────────
# STEP — turnover-style incremental scoring
# ──────────────────────────────────────────────────────────────────────────────

def apply_step(criteria_text: str, max_marks: int, value_str: str) -> tuple[float, str]:
    """
    Handles patterns like:
      "Turnover ≥ 50 Cr = 5 marks; for every additional 10 Cr = 1 mark"
      "Up to 100 Cr = 10 marks; above 100 Cr, 1 mark per 20 Cr"
      "< 50 Cr = 0; 50–100 Cr = 5; > 100 Cr = 10"

    Strategy: parse ALL (threshold, marks) pairs; build sorted band list.
    If no pairs found, fall back to BAND.
    """
    ct = criteria_text

    # Try to parse "step" pattern: base + increment
    base_m = re.search(
        r"(\d+(?:\.\d+)?)\s*cr[ores]*[\s.=:\-–]+(\d+(?:\.\d+)?)\s*marks?",
        ct, re.I,
    )
    step_m = re.search(
        r"(?:every|each|per)\s+additional\s+(\d+(?:\.\d+)?)\s*cr[ores]*"
        r"[\s\W]+(\d+(?:\.\d+)?)\s*marks?",
        ct, re.I,
    )

    value = _first_num(value_str)
    if value is None:
        return 0.0, f"STEP: could not parse value from '{value_str}'"

    if base_m and step_m:
        try:
            base_thresh = float(base_m.group(1))
            base_score  = float(base_m.group(2))
            step_size   = float(step_m.group(1))
            step_score  = float(step_m.group(2))
            if value < base_thresh:
                s = 0.0
            else:
                extra = int((value - base_thresh) / step_size)
                s = base_score + extra * step_score
            s = round(min(s, max_marks), 1)
            return s, (f"STEP: value={value} Cr, base≥{base_thresh} Cr={base_score} marks, "
                       f"+{step_score} per {step_size} Cr → {s}/{max_marks}")
        except (ValueError, ZeroDivisionError):
            pass

    # Fall back to BAND interpretation
    return apply_band(ct, max_marks, value_str, unit_label="Cr")


# ──────────────────────────────────────────────────────────────────────────────
# BAND — threshold band scoring
# ──────────────────────────────────────────────────────────────────────────────

def apply_band(criteria_text: str, max_marks: int,
               value_str: str, unit_label: str = "") -> tuple[float, str]:
    """
    Handles patterns like:
      "6 professionals = 10; 7-12 = 15; >12 = 20"
      "≤ 50 Cr = 5; 50-100 Cr = 10; > 100 Cr = 15"
      "More than 6 and up to 12 employees: 15 marks"

    Builds a sorted list of (upper_bound, marks); awards score of the
    first band whose upper_bound >= value.
    """
    ct = criteria_text
    bands: list[tuple[float, float]] = []  # (upper_bound, marks)

    # Pattern: "N <unit> = M marks" or "up to N <unit>: M marks"
    for mm in re.finditer(
        r"(?:up\s+to|of|=|:)?\s*(\d+(?:\.\d+)?)\s*(?:cr[ores]*|professionals?|"
        r"employees?|persons?)?\s*[=:\-–]\s*(\d+(?:\.\d+)?)\s*marks?",
        ct, re.I,
    ):
        upper = float(mm.group(1))
        score = float(mm.group(2))
        if 0 < score <= max_marks:
            bands.append((upper, score))

    # Pattern: "more than N [and up to M]: K marks"
    for mm in re.finditer(
        r"more\s+than\s+(\d+(?:\.\d+)?)(?:\s+and\s+up\s+to\s+(\d+(?:\.\d+)?))?"
        r"\s*(?:cr[ores]*|professionals?|employees?)?\s*[:\-–]\s*(\d+(?:\.\d+)?)\s*marks?",
        ct, re.I,
    ):
        lo   = float(mm.group(1))
        hi   = float(mm.group(2)) if mm.group(2) else 1e9
        score = float(mm.group(3))
        if 0 < score <= max_marks:
            bands.append((hi, score))

    if not bands:
        return 0.0, f"BAND: no bands parsed from criteria"

    bands.sort(key=lambda b: b[0])
    value = _first_num(value_str)
    if value is None:
        return 0.0, f"BAND: could not parse value from '{value_str}'"

    awarded = 0.0
    for upper, score in bands:
        if value <= upper:
            awarded = score
            break
    else:
        awarded = bands[-1][1]  # exceeds all defined thresholds

    awarded = round(min(awarded, max_marks), 1)
    return awarded, (f"BAND: value={value}{unit_label}, "
                     f"bands={[(b, s) for b, s in bands[:4]]} → {awarded}/{max_marks}")


# ──────────────────────────────────────────────────────────────────────────────
# PER_UNIT — N marks per qualifying project/assignment
# ──────────────────────────────────────────────────────────────────────────────

def apply_per_unit(criteria_text: str, max_marks: int,
                   value_str: str) -> tuple[float, str]:
    """
    Handles patterns like:
      "5 marks for each qualifying project (max 20)"
      "4 marks per assignment, maximum 16 marks"
      "10 marks per project — only first 3 considered"
    """
    ct = criteria_text

    rate_m = re.search(
        r"(\d+(?:\.\d+)?)\s*marks?\s+(?:for\s+(?:each|01|per|one|every)|per)\s+"
        r"(?:qualifying\s+)?(?:project|assignment|work\s+order)",
        ct, re.I,
    )
    if not rate_m:
        rate_m = re.search(
            r"(\d+(?:\.\d+)?)\s*marks?\s+(?:is\s+)?awarded\s+for\s+each",
            ct, re.I,
        )

    if not rate_m:
        return 0.0, "PER_UNIT: could not parse rate from criteria"

    rate = float(rate_m.group(1))
    count = _first_num(value_str)
    if count is None:
        return 0.0, f"PER_UNIT: could not parse count from '{value_str}'"

    # Check if criteria caps the number of projects considered
    cap_m = re.search(r"(?:only\s+(?:first|top)|maximum\s+of)\s+(\d+)\s+"
                      r"(?:projects?|assignments?)\s+(?:shall\s+be\s+)?considered",
                      ct, re.I)
    if cap_m:
        count = min(count, float(cap_m.group(1)))

    score = round(min(count * rate, max_marks), 1)
    return score, (f"PER_UNIT: {count} qualifying × {rate} marks = "
                   f"{count * rate:.1f}, capped at {max_marks} → {score}")


# ──────────────────────────────────────────────────────────────────────────────
# QUAL — Qualification / CV evidence check (structured)
# ──────────────────────────────────────────────────────────────────────────────

def apply_qual_structured(evidence: dict, max_marks: int) -> tuple[float, str]:
    """
    evidence dict from the proposal scorer:
    {
        "named_experts": bool,
        "education_stated": bool,
        "experience_years_stated": bool,
        "relevant_projects_listed": bool,
        "cvs_attached": bool,
        "notes": str,
    }
    Weighted evidence check — no LLM needed.
    """
    weights = {
        "named_experts":          0.10,
        "education_stated":       0.25,
        "experience_years_stated": 0.25,
        "relevant_projects_listed": 0.25,
        "cvs_attached":           0.15,
    }
    total_w = sum(w for k, w in weights.items() if evidence.get(k, False))
    score   = round(total_w * max_marks, 1)
    found   = [k for k in weights if evidence.get(k, False)]
    missing = [k for k in weights if not evidence.get(k, False)]
    return score, (
        f"QUAL: found={found}, missing={missing}, "
        f"weight={total_w:.2f} × {max_marks} = {score}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# BINARY — yes/no criteria
# ──────────────────────────────────────────────────────────────────────────────

def apply_binary(found: bool, max_marks: int, label: str = "") -> tuple[float, str]:
    score = float(max_marks) if found else 0.0
    return score, f"BINARY: {'found' if found else 'not found'} {label} → {score}/{max_marks}"


# ──────────────────────────────────────────────────────────────────────────────
# Score clamping helper
# ──────────────────────────────────────────────────────────────────────────────

def clamp(score: float, max_marks: int) -> float:
    return round(max(0.0, min(float(score), float(max_marks))), 1)


# ──────────────────────────────────────────────────────────────────────────────
# Strict band parser — used by tq_compliance_parser._apply_rfp_formula
# Handles ALL common Indian-RFP scoring patterns.
# ──────────────────────────────────────────────────────────────────────────────

def _parse_band_table_strict(
    criteria_text: str,
    formula_type: str = "BAND",
) -> list:
    """
    Parse scoring bands from RFP criteria text.

    Handles:
      "100-200 Crs: 5 marks"          → {min:100, max:200, score:5}
      "Up to 100 Cr = 5 marks"        → {min:0,   max:100, score:5}
      "Upto 500 Crs = 10 marks"       → {min:0,   max:500, score:10}
      "Between 100 and 200 Cr: 10 marks" → {min:100, max:200, score:10}
      "More than 4 projects: 10 marks" → {min:4,  max:None, score:10}
      "Above 300 Crs: 15 marks"       → {min:300, max:None, score:15}
      "≥ 50 Cr = 5 marks"             → {min:50,  max:None, score:5}
      "N+: S marks"                    → {min:N,  max:None, score:S}
      "> 200 Crs: 10 marks"           → {min:200, max:None, score:10}

    Returns sorted list of band dicts, or [] if nothing parseable.
    """
    ct = criteria_text or ""

    def _n(s: str) -> float:
        return float(str(s).replace(",", ""))

    bands: list[dict] = []

    # Trailing "marks?" is optional so the parser works when RFP omits the word
    _UNIT = r'(?:cr[ores]*|lakh|years?|projects?|assignments?|persons?|professionals?|employees?)?'
    _SCORE = r'(\d+(?:\.\d+)?)\s*(?:marks?)?'
    _SEP   = r'\s*[:\-–=]\s*'

    # 1. "Up to / Upto / ≤ / <= X [unit]: N [marks]"
    for m in re.finditer(
        rf'(?:up\s*to|upto|≤|<=)\s*(\d[\d,]*(?:\.\d+)?)\s*{_UNIT}\s*{_SEP}{_SCORE}',
        ct, re.I,
    ):
        sc = float(m.group(2))
        if sc > 0:
            bands.append({"min": 0.0, "max": _n(m.group(1)), "score": sc})

    # 2. "X to Y / X - Y [unit]: N [marks]"
    for m in re.finditer(
        rf'(\d[\d,]*(?:\.\d+)?)\s*(?:to|-|–)\s*(\d[\d,]*(?:\.\d+)?)\s*{_UNIT}\s*{_SEP}{_SCORE}',
        ct, re.I,
    ):
        lo = _n(m.group(1))
        hi = _n(m.group(2))
        sc = float(m.group(3))
        if lo < hi and 0 < sc:
            bands.append({"min": lo, "max": hi, "score": sc})

    # 3. "Between X and Y [unit]: N [marks]"
    for m in re.finditer(
        rf'between\s+(\d[\d,]*(?:\.\d+)?)\s+and\s+(\d[\d,]*(?:\.\d+)?)\s*{_UNIT}\s*{_SEP}{_SCORE}',
        ct, re.I,
    ):
        lo = _n(m.group(1))
        hi = _n(m.group(2))
        sc = float(m.group(3))
        if 0 < sc:
            bands.append({"min": lo, "max": hi, "score": sc})

    # 4. "More than / Above / > / ≥ / >= X [unit]: N [marks]"
    for m in re.finditer(
        rf'(?:more\s+than|above|over|>\s*|greater\s+than|≥|>=|at\s+least|minimum\s+of?)\s*'
        rf'(\d[\d,]*(?:\.\d+)?)\s*{_UNIT}\s*{_SEP}{_SCORE}',
        ct, re.I,
    ):
        lo = _n(m.group(1))
        sc = float(m.group(2))
        if sc > 0:
            bands.append({"min": lo, "max": None, "score": sc})

    # 5. "N+ [unit]: S [marks]"
    for m in re.finditer(
        rf'(\d[\d,]*(?:\.\d+)?)\s*\+\s*{_UNIT}\s*{_SEP}{_SCORE}',
        ct, re.I,
    ):
        lo = _n(m.group(1))
        sc = float(m.group(2))
        if sc > 0 and not any(
            abs(b["min"] - lo) < 0.1 and b["max"] is None for b in bands
        ):
            bands.append({"min": lo, "max": None, "score": sc})

    # 6. Simple "N [unit] = S [marks]" without range
    if not bands:
        for m in re.finditer(
            rf'(\d[\d,]*(?:\.\d+)?)\s*{_UNIT}\s*[=:]\s*{_SCORE}',
            ct, re.I,
        ):
            lo = _n(m.group(1))
            sc = float(m.group(2))
            if 0 < sc <= 100:
                bands.append({"min": lo, "max": None, "score": sc})

    # Deduplicate by (min, max) keeping highest score for duplicates
    seen: dict = {}
    for b in bands:
        key = (b["min"], b["max"])
        if key not in seen or b["score"] > seen[key]["score"]:
            seen[key] = b

    result = sorted(seen.values(), key=lambda b: float(b["min"]))
    return result


def _apply_band_strict(
    bands: list,
    value: float,
    max_marks: int,
    formula_type: str = "BAND",
) -> Optional[float]:
    """
    Apply band scoring deterministically.

    Boundary convention:
      - Closed bands [lo, hi): lo <= value < hi
      - Open-ended top band (max=None): value >= lo
      - Walk ALL bands ascending; LAST match wins
        (so "more than 300" beats "up to 300" when value == 301)

    Returns score clamped to max_marks, or None if no bands supplied.
    """
    if not bands:
        return None

    matched: Optional[float] = None
    for band in sorted(bands, key=lambda b: float(b.get("min") or 0)):
        lo     = float(band.get("min") or 0)
        hi_raw = band.get("max")
        hi     = float(hi_raw) if hi_raw is not None else float("inf")
        sc     = float(band.get("score", 0))

        if hi_raw is None:
            if value >= lo:
                matched = sc
        else:
            if lo <= value < hi:
                matched = sc

    if matched is None:
        # value is below the first band minimum → 0 marks
        sorted_bands = sorted(bands, key=lambda b: float(b.get("min") or 0))
        if value >= float(sorted_bands[-1].get("min") or 0):
            # value exceeds all defined thresholds → award the top band score
            return round(min(float(sorted_bands[-1].get("score", 0)), float(max_marks)), 1)
        return 0.0

    return round(min(matched, float(max_marks)), 1)


def _apply_step(
    criteria_text: str,
    max_marks: int,
    value: float,
) -> Optional[float]:
    """
    Apply STEP formula: base marks + per-additional-unit increment.
    Returns score or None if the step pattern cannot be parsed.

    Handles:
      "Turnover ≥ 50 Cr = 5 marks; for every additional 10 Cr = 1 mark"
      "100 Cr = 5 marks; above 100 Cr, 1 mark per 20 Cr additional"
    """
    ct = criteria_text or ""

    base_m = re.search(
        r'(\d+(?:\.\d+)?)\s*cr[ores]*\s*[=:\-–]\s*(\d+(?:\.\d+)?)\s*marks?',
        ct, re.I,
    )
    step_m = re.search(
        r'(?:every|each|per)\s+additional\s+(\d+(?:\.\d+)?)\s*cr[ores]*'
        r'[\s\W]+(\d+(?:\.\d+)?)\s*marks?',
        ct, re.I,
    )

    if not (base_m and step_m):
        return None

    try:
        base_thresh = float(base_m.group(1))
        base_score  = float(base_m.group(2))
        step_size   = float(step_m.group(1))
        step_sc     = float(step_m.group(2))
        if step_size <= 0:
            return None
        if value < base_thresh:
            return 0.0
        steps = int((value - base_thresh) / step_size)
        score = base_score + steps * step_sc
        return round(min(score, float(max_marks)), 1)
    except (ValueError, ZeroDivisionError):
        return None


def _detect_formula(parameter: str, criteria_text: str) -> str:
    """
    Detect formula type from criterion name + criteria text.
    Thin wrapper around tq_extractor_v20.detect_formula_type so callers
    that already imported tq_formula_engine don't need another import.
    """
    try:
        from core.tq_extractor_v20 import detect_formula_type
        return detect_formula_type(parameter, criteria_text)
    except ImportError:
        pass

    ct = (criteria_text or "").lower()
    p  = (parameter or "").lower()

    if re.search(r'\d+\s*marks?\s+(?:per|for\s+(?:each|every|01))\s+'
                 r'(?:project|assignment)', ct):
        return "PER_UNIT"
    if re.search(r'(?:per|for\s+every)\s+additional', ct):
        return "STEP"
    if re.search(r'(?:registered|certified|iso|methodology|work\s+plan)', ct):
        return "BINARY"
    if re.search(r'(?:team\s+leader|expert|specialist|key\s+personnel)', p):
        return "QUAL"
    return "BAND"
