"""
core/tq_extractor_v20.py  —  v20  RFP Marking-Scheme Extractor
===============================================================

The canonical entry point for RFP criteria extraction.
Wraps tq_step1_extract (deterministic PyMuPDF geometry) and rfp_cache,
adding formula-type detection and pre-computed scoring bands.

Called by:
    tq_compliance_parser.run_tq_evaluation()
    routes.py background task (via run_tq_evaluation)

Returned shape
--------------
{
    "criteria": [
        {
            "item_code":    str,
            "parameter":    str,
            "max_marks":    int,
            "criteria_text": str,
            "formula_type": "BAND"|"STEP"|"PER_UNIT"|"QUAL"|"BINARY"|"LLM",
            "search_keywords": list[str],
            "is_parent":    bool,
            "is_sub_item":  bool,
        },
        ...
    ],
    "bands":                    dict,   # {parameter: formula_dict}
    "grand_total_marks":        int,
    "live_assessment_marks":    int,
    "live_assessment_label":    str,
    "qualification_threshold_pct": float,
    "doc_max":                  int,
    "error":                    str|None,
}
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Formula-type detector
# ─────────────────────────────────────────────────────────────────────────────

_PER_UNIT_RE = re.compile(
    r'\d+(?:\.\d+)?\s*marks?\s+(?:per|for\s+(?:each|every|01|one))\s+'
    r'(?:qualifying\s+)?(?:project|assignment|work\s+order)',
    re.I,
)
_STEP_RE = re.compile(
    r'(?:per|for\s+every|for\s+each)\s+additional',
    re.I,
)
_BAND_THRESHOLD_RE = re.compile(
    r'(?:more\s+than|up\s*to|upto|between|above|below|less\s+than|≤|≥|<=|>=)',
    re.I,
)
_QUAL_ROLE_RE = re.compile(
    r'(?:team\s+leader|procurement\s+expert|documentation\s+expert|'
    r'urban\s+plan|environmental\s+expert|ict|gis\s+expert|data\s+analyst|'
    r'legal\s+policy|finance\s+expert|reporting\s+manager|liaison\s+officer|'
    r'ppp\s+specialist|social\s+development|monitoring\s+expert|key\s+(?:staff|personnel|expert))',
    re.I,
)
_BINARY_RE = re.compile(
    r'(?:registered\s+with|empanelled|certified|iso\s+\d|quality\s+management|'
    r'methodology|approach\s+paper|work\s+plan|presence\s+of)',
    re.I,
)


def detect_formula_type(parameter: str, criteria_text: str) -> str:
    """
    Deterministic formula-type detection from parameter name and criteria text.
    Returns one of: BAND | STEP | PER_UNIT | QUAL | BINARY | LLM
    """
    ct = criteria_text or ""
    p  = parameter or ""

    # PER_UNIT: marks per project/assignment
    if _PER_UNIT_RE.search(ct):
        return "PER_UNIT"

    # STEP: base + per-additional-increment
    if _STEP_RE.search(ct):
        return "STEP"

    # QUAL: key personnel criteria scored by CV presence
    if _QUAL_ROLE_RE.search(p):
        return "QUAL"

    # BINARY: simple yes/no presence check
    if _BINARY_RE.search(ct) and not re.search(r'\d+\s*to\s*\d+', ct):
        return "BINARY"

    # BAND: threshold-based or any numeric range scoring
    if (_BAND_THRESHOLD_RE.search(ct)
            and re.search(r'\d+\s*marks?', ct, re.I)):
        return "BAND"

    # Default BAND for quantitative criteria (turnover, projects, experience)
    if re.search(r'(?:turnover|crore|project|assignment|experience|manpower|'
                 r'professional|personnel|revenue|net\s+worth|years?\s+of)',
                 ct, re.I):
        return "BAND"

    return "LLM"


_KW_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "have",
    "been", "each", "into", "over", "will", "are", "not", "its",
    "per", "any", "marks", "mark", "criteria", "criterion",
    "maximum", "minimum", "shall", "should", "must",
})


def build_search_keywords(parameter: str, criteria_text: str) -> list[str]:
    """Extract search keywords for proposal page ranking."""
    combined = f"{parameter} {criteria_text}".lower()
    words = re.findall(r'\b[a-z]{4,}\b', combined)
    unique = list(dict.fromkeys(w for w in words if w not in _KW_STOPWORDS))
    return unique[:12]


# ─────────────────────────────────────────────────────────────────────────────
# Live-assessment detection
# ─────────────────────────────────────────────────────────────────────────────

_LIVE_SIGNALS = re.compile(
    r'(?:presentation|interview|viva|panel\s+discussion|demo\s+to\s+client)',
    re.I,
)


def _detect_live_marks(criteria: list[dict]) -> tuple[int, str]:
    """
    Identify criteria that require live assessment (presentation/interview).
    Returns (total_live_marks, label).
    """
    live_total = 0
    live_labels: list[str] = []
    for c in criteria:
        if _LIVE_SIGNALS.search(c.get("parameter", "") + " " + c.get("criteria_text", "")):
            live_total += int(c.get("max_marks") or 0)
            live_labels.append(c.get("parameter", ""))
    label = " / ".join(live_labels[:3]) if live_labels else ""
    return live_total, label


# ─────────────────────────────────────────────────────────────────────────────
# RFP file discovery
# ─────────────────────────────────────────────────────────────────────────────

_SEARCH_DIRS = [
    Path("./uploads"),
    Path("./tq_uploads"),
    Path("."),
]


def _find_rfp_path(rfp_doc_name: str) -> Optional[str]:
    for d in _SEARCH_DIRS:
        candidate = d / rfp_doc_name
        if candidate.exists():
            return str(candidate)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_marking_table(
    rfp_doc_name: str,
    force_refresh: bool = False,
) -> dict:
    """
    Extract and cache the TQ marking scheme from an RFP PDF.

    Pipeline:
      1. Find RFP file.
      2. Load from rfp_cache (skip extraction if valid cache exists).
      3. If not cached: call tq_step1_extract.extract_marking_scheme.
      4. Detect formula types for each criterion.
      5. Build search keywords.
      6. Detect live-assessment criteria.
      7. Pre-compute scoring bands (one LLM call per criterion).
      8. Save to rfp_cache.
      9. Return full result dict.
    """
    # Step 1: find file
    rfp_path = _find_rfp_path(rfp_doc_name)
    if not rfp_path:
        print(f"[v20] ERROR: RFP file not found: {rfp_doc_name}")
        return _empty(f"RFP file not found: {rfp_doc_name}")

    # Step 2: try cache
    if not force_refresh:
        try:
            from core.rfp_cache import load_cache
            cached = load_cache(rfp_path)
            if cached and cached.get("criteria"):
                print(f"[v20] Cache hit: {len(cached['criteria'])} criteria")
                return _cache_to_result(cached)
        except Exception as e:
            print(f"[v20] Cache load error (non-fatal): {e}")

    # Step 3: extract from PDF
    print(f"[v20] Extracting marking scheme: {rfp_doc_name}")
    try:
        from core.tq_step1_extract import extract_marking_scheme
        raw = extract_marking_scheme(rfp_doc_name)
    except Exception as e:
        print(f"[v20] tq_step1_extract failed: {e}")
        raw = {}

    criteria_raw = raw.get("criteria", [])
    if not criteria_raw:
        # Fallback: try tq_criteria_extractor (LLM-assisted)
        print("[v20] Step1 returned 0 criteria — trying tq_criteria_extractor")
        try:
            from core.tq_criteria_extractor import extract_marking_scheme as _alt
            alt = _alt(rfp_path)
            criteria_raw = alt if isinstance(alt, list) else alt.get("criteria", [])
        except Exception as e2:
            print(f"[v20] tq_criteria_extractor also failed: {e2}")

    if not criteria_raw:
        return _empty("No criteria extracted from RFP — check PDF structure")

    # Step 4+5: enrich each criterion
    enriched: list[dict] = []
    for c in criteria_raw:
        parameter    = c.get("parameter", "")
        criteria_txt = c.get("criteria_text", "")
        formula_type = detect_formula_type(parameter, criteria_txt)
        keywords     = build_search_keywords(parameter, criteria_txt)
        enriched.append({
            "item_code":       str(c.get("item_code", "")),
            "parameter":       parameter,
            "max_marks":       int(c.get("max_marks") or 0),
            "criteria_text":   criteria_txt,
            "formula_type":    formula_type,
            "search_keywords": keywords,
            "is_parent":       bool(c.get("is_parent", False)),
            "is_sub_item":     bool(c.get("is_sub_item", False)),
        })

    # Step 6: detect live marks
    live_marks, live_label = _detect_live_marks(enriched)

    # Grand total and doc max
    grand_total = raw.get("grand_total_marks") or sum(
        c["max_marks"] for c in enriched
    )
    doc_max = grand_total - live_marks
    threshold = float(raw.get("qualification_threshold") or 70.0)

    print(f"[v20] {len(enriched)} criteria | grand_total={grand_total} "
          f"| live={live_marks} | doc_max={doc_max} | threshold={threshold}%")
    for c in enriched:
        print(f"  [{c['item_code']:3}] {c['parameter'][:50]:52} "
              f"{c['max_marks']:3}  {c['formula_type']}")

    # Step 7: pre-compute scoring bands (one LLM call per criterion)
    bands: dict = {}
    try:
        from core.rfp_cache import precompute_bands
        bands = precompute_bands(enriched)
    except Exception as e:
        print(f"[v20] Band precompute error (non-fatal): {e}")
        # Regex fallback for all criteria
        try:
            from core.rfp_cache import _regex_parse_bands
            for c in enriched:
                if not c.get("is_parent") and c.get("criteria_text"):
                    rb = _regex_parse_bands(c["criteria_text"], c["formula_type"])
                    if rb:
                        bands[c["parameter"]] = rb
        except Exception:
            pass

    extraction = {
        "criteria":                  enriched,
        "grand_total_marks":         grand_total,
        "live_assessment_marks":     live_marks,
        "live_assessment_label":     live_label,
        "doc_max":                   doc_max,
        "qualification_threshold_pct": threshold,
        "context_source":            "tq_step1_extract",
        "error":                     None,
    }

    # Step 8: save to cache
    try:
        from core.rfp_cache import save_cache
        save_cache(rfp_path, extraction, bands)
    except Exception as e:
        print(f"[v20] Cache save error (non-fatal): {e}")

    extraction["bands"] = bands
    return extraction


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cache_to_result(cached: dict) -> dict:
    """Convert rfp_cache format to extract_marking_table return format."""
    criteria = cached.get("criteria", [])
    # Back-fill formula_type / search_keywords if cache was written by old code
    for c in criteria:
        if not c.get("formula_type"):
            c["formula_type"] = detect_formula_type(
                c.get("parameter", ""), c.get("criteria_text", "")
            )
        if not c.get("search_keywords"):
            c["search_keywords"] = build_search_keywords(
                c.get("parameter", ""), c.get("criteria_text", "")
            )
    return {
        "criteria":                    criteria,
        "bands":                       cached.get("bands", {}),
        "grand_total_marks":           cached.get("grand_total") or 0,
        "live_assessment_marks":       cached.get("live_marks") or 0,
        "live_assessment_label":       cached.get("live_label") or "",
        "doc_max":                     cached.get("doc_total") or 0,
        "qualification_threshold_pct": float(cached.get("threshold") or 70.0),
        "context_source":              "cache",
        "error":                       None,
    }


def _empty(msg: str) -> dict:
    return {
        "criteria":                    [],
        "bands":                       {},
        "grand_total_marks":           0,
        "live_assessment_marks":       0,
        "live_assessment_label":       "",
        "doc_max":                     0,
        "qualification_threshold_pct": 70.0,
        "context_source":              "",
        "error":                       msg,
    }
