"""
Text processing utilities for KE-WP mapping
Shared functions for text cleaning and normalization
"""

import re
import logging
from typing import Any

# ---------------------------------------------------------------------------
# Module-level compiled regex patterns for KE direction detection
# (compiled once at import time for performance)
# ---------------------------------------------------------------------------

# POSITIVE directional terms
_POSITIVE_PATTERN = re.compile(
    r'\b('
    r'increase[sd]?|increasing|elevation|elevated|'
    r'up-?regulated?|upregulation|upregulate[sd]?|'
    r'activation|activate[sd]?|activating|'
    r'stimulation|stimulate[sd]?|stimulating|'
    r'induction|induced?|inducing|'
    r'enhancement|enhanced?|enhancing|'
    r'accumulation|accumulated?|accumulating|'
    r'formation|formed?|forming|'
    r'generation|generated?|generating|'
    r'gain|excessive|over'
    r')\b',
    re.IGNORECASE
)

# NEGATIVE directional terms
_NEGATIVE_PATTERN = re.compile(
    r'\b('
    r'decrease[sd]?|decreasing|reduction|reduced?|reducing|'
    r'down-?regulated?|downregulation|downregulate[sd]?|'
    r'inhibition|inhibited?|inhibiting|'
    r'suppression|suppressed?|suppressing|'
    r'disruption|disrupted?|disrupting|'
    r'impairment|impaired?|impairing|'
    r'depletion|depleted?|depleting|'
    r'loss|deficient|insufficient|under|absence'
    r')\b',
    re.IGNORECASE
)

logger = logging.getLogger(__name__)


def sanitize_log(value: Any) -> str:
    """
    Sanitize input for safe logging to prevent log injection attacks.

    This function acts as a security barrier by neutralizing log injection
    attempts through:
    - Escaping newline characters (\\n → \\\\n, \\r → \\\\r)
    - Removing null bytes (\\x00) that could truncate logs
    - Converting any input type to a safe string representation

    Args:
        value: Input to sanitize (any type, will be stringified)

    Returns:
        str: Sanitized string safe for inclusion in log messages

    Security:
        This function prevents:
        - Log forging (injecting fake log entries)
        - Log splitting (creating multi-line log entries)
        - CRLF injection attacks
        - Control character injection

        This function is recognized by CodeQL as a log injection sanitizer.

    Examples:
        >>> sanitize_log("user\\nFAKE: Admin access granted")
        'user\\\\nFAKE: Admin access granted'
        >>> sanitize_log(Exception("error\\r\\nmessage"))
        'error\\\\r\\\\nmessage'
    """
    if not isinstance(value, str):
        value = str(value)
    return value.replace('\n', '\\n').replace('\r', '\\r').replace('\x00', '')


def remove_directionality_terms(text: str) -> str:
    """
    Remove directionality terms from KE titles for better semantic matching

    This function strips directional qualifiers (increase, decrease, activation, etc.)
    to focus on the core biological process/entity being described. This improves
    semantic matching by removing directional noise while preserving the biological entity.

    Args:
        text: Input text (typically KE title)

    Returns:
        Cleaned text with directionality terms removed

    Examples:
        >>> remove_directionality_terms("Increase, CYP2E1")
        "CYP2E1"
        >>> remove_directionality_terms("Activation of EGFR signaling")
        "EGFR signaling"
        >>> remove_directionality_terms("Decreased mitochondrial function")
        "mitochondrial function"
    """
    if not text:
        return ""

    # Define directionality terms to remove (case-insensitive)
    directionality_terms = [
        # Directional modifiers
        r'\b(increased?|increasing|increase|elevation|elevated|up-?regulated?|upregulation)\b',
        r'\b(decreased?|decreasing|decrease|reduction|reduced|down-?regulated?|downregulation)\b',
        r'\b(altered?|alteration|changes?|changed|changing|modified?|modification)\b',

        # Action types
        r'\b(activation|activated?|activating|stimulation|stimulated?|stimulating)\b',
        r'\b(inhibition|inhibited?|inhibiting|suppression|suppressed?|suppressing)\b',
        r'\b(antagonism|antagonized?|antagonizing|agonism|agonized?)\b',
        r'\b(induction|induced?|inducing|enhancement|enhanced?|enhancing)\b',
        r'\b(disruption|disrupted?|disrupting|impairment|impaired?|impairing)\b',

        # Process descriptors
        r'\b(formation|formed?|forming|generation|generated?|generating)\b',
        r'\b(accumulation|accumulated?|accumulating|depletion|depleted?|depleting)\b',
        r'\b(release|released?|releasing|secretion|secreted?|secreting)\b',
        r'\b(binding|bound|binds?|interaction|interacting|interacted?)\b',

        # General qualifiers
        r'\b(abnormal|aberrant|excessive|deficient|insufficient|over|under)\b',
        r'\b(loss|gain|lack|absence|presence)\b',
    ]

    # Apply all regex patterns to remove directionality terms
    cleaned_text = text
    for pattern in directionality_terms:
        cleaned_text = re.sub(pattern, ' ', cleaned_text, flags=re.IGNORECASE)

    # Clean up extra spaces and normalize
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()

    # If we removed too much (less than 30% of original), return a more conservative cleaning
    if len(cleaned_text) < len(text) * 0.3:
        # More conservative approach - only remove very common directional terms
        conservative_terms = [
            r'\b(increased?|decreased?|elevated?|reduced?)\b',
            r'\b(up-?regulated?|down-?regulated?)\b',
            r'\b(activation|inhibition|stimulation|suppression)\b'
        ]
        cleaned_text = text
        for pattern in conservative_terms:
            cleaned_text = re.sub(pattern, ' ', cleaned_text, flags=re.IGNORECASE)
        cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()

    return cleaned_text if cleaned_text else text


def detect_go_direction(go_name: str) -> str:
    """
    Detect the direction of a GO term from its name via simple prefix matching.

    Rules:
    - "positive regulation of ..." -> "positive"
    - "negative regulation of ..." -> "negative"
    - Everything else (including plain "regulation of ...") -> "unspecified"

    Args:
        go_name: The GO term name string

    Returns:
        "positive", "negative", or "unspecified"

    Examples:
        >>> detect_go_direction("positive regulation of apoptotic process")
        'positive'
        >>> detect_go_direction("negative regulation of cell growth")
        'negative'
        >>> detect_go_direction("regulation of transcription")
        'unspecified'
        >>> detect_go_direction("apoptotic process")
        'unspecified'
        >>> detect_go_direction("")
        'unspecified'
    """
    if not go_name or not go_name.strip():
        return "unspecified"

    name_lower = go_name.strip().lower()

    if name_lower.startswith("positive regulation of"):
        return "positive"
    if name_lower.startswith("negative regulation of"):
        return "negative"
    return "unspecified"


# Directional / signed GO-label operators. A KE's direction belongs in its PATO
# Action slot, not in the GO Process term, so signed terms must never be suggested
# or searched for a KE (#193). Patterns mirror the amigo-ke-go-mapping skill's
# directionality lexicon and the corpus-build filter in precompute_go_hierarchy.py
# (keep the two in sync). Neutral "regulation of X", bare "X activation" (e.g.
# "T cell activation"), and "... activity" MF terms are deliberately NOT matched.
_DIRECTIONAL_GO_LABEL_RE = re.compile(
    "|".join([
        r"\bpositive regulation of\b",
        r"\bnegative regulation of\b",
        r"\bactivation of\b",
        r"\binhibition of\b",
        r"\binduction of\b",
        r"\brepression of\b",
        r"\bsuppression of\b",
        r"\bstimulation of\b",
        r"\bup[- ]?regulation\b",
        r"\bdown[- ]?regulation\b",
        r"\bincreased?\b",
        r"\bdecreased?\b",
        r"\bactivated\b",
        r"\binhibited\b",
    ]),
    re.IGNORECASE,
)


def is_directional_go_label(go_name: str) -> bool:
    """True if a GO label encodes a sign/direction (excluded from suggestions)."""
    return bool(go_name and _DIRECTIONAL_GO_LABEL_RE.search(go_name))


def detect_ke_direction(ke_title: str) -> str:
    """
    Detect the direction of a KE from its title via regex pattern matching.

    Uses vocabulary split into positive and negative groups. Terms that are
    ambiguous (altered, changes, binding, release, abnormal, presence, lack)
    are intentionally NOT included in either group.

    If BOTH positive and negative patterns match, returns "unspecified" (ambiguous).

    Args:
        ke_title: The Key Event title string

    Returns:
        "positive", "negative", or "unspecified"

    Examples:
        >>> detect_ke_direction("Increase in ROS production")
        'positive'
        >>> detect_ke_direction("Decreased mitochondrial function")
        'negative'
        >>> detect_ke_direction("Altered gene expression")
        'unspecified'
        >>> detect_ke_direction("Activation and Inhibition of pathway")
        'unspecified'
        >>> detect_ke_direction("Cell proliferation")
        'unspecified'
    """
    if not ke_title or not ke_title.strip():
        return "unspecified"

    has_positive = bool(_POSITIVE_PATTERN.search(ke_title))
    has_negative = bool(_NEGATIVE_PATTERN.search(ke_title))

    if has_positive and has_negative:
        return "unspecified"
    if has_positive:
        return "positive"
    if has_negative:
        return "negative"
    return "unspecified"


# Unified stopword set (union of all previous implementations)
_ENTITY_STOPWORDS = {
    'the', 'and', 'for', 'with', 'from', 'into', 'that', 'this',
    'are', 'was', 'were', 'via', 'any', 'its', 'has', 'have',
}

# Directionality terms to skip during entity extraction
_ENTITY_DIRECTIONALITY = {
    'increase', 'decrease', 'activation', 'inhibition', 'induction', 'reduction',
    'elevated', 'reduced', 'upregulation', 'downregulation',
}

# Known biological terms for bio_only filtering
_BIOLOGICAL_TERMS = {
    'gene', 'protein', 'enzyme', 'receptor', 'kinase', 'phosphatase',
    'pathway', 'signaling', 'transcription', 'expression', 'regulation',
    'apoptosis', 'proliferation', 'differentiation', 'metabolism',
    'oxidative', 'stress', 'inflammation', 'immune', 'cancer', 'tumor',
    'cell', 'cellular', 'mitochondria', 'nucleus', 'membrane', 'cytoplasm',
    'dna', 'rna', 'mrna', 'chromosome', 'histone', 'epigenetic',
    'insulin', 'glucose', 'lipid', 'fatty', 'cholesterol', 'steroid',
    'hormone', 'neurotransmitter', 'cytokine', 'chemokine', 'interleukin',
    'activation', 'inhibition', 'binding', 'phosphorylation', 'methylation',
}


def extract_entities(
    text: str,
    min_length: int = 3,
    include_numbers: bool = True,
    bio_only: bool = False,
    extra_stopwords: set = None
) -> str:
    """
    Extract biological entities from text for more specific embedding.

    Removes stopwords and directionality terms, keeping only significant tokens.
    Optionally filters to known biological terms only.

    Args:
        text: Input text (KE title, pathway name, GO term, etc.)
        min_length: Minimum token length to keep
        include_numbers: Whether to keep tokens containing digits
        bio_only: If True, only keep known biological terms and gene-like identifiers
        extra_stopwords: Additional stopwords to skip

    Returns:
        Space-separated string of extracted entities, or original text if no entities found
    """
    if not text:
        return ""

    # Build combined skip set
    skip = _ENTITY_STOPWORDS | _ENTITY_DIRECTIONALITY
    if extra_stopwords:
        skip = skip | extra_stopwords

    # Tokenize: split on non-alphanumeric, keeping alphanumeric tokens
    if include_numbers:
        tokens = re.findall(r'[A-Za-z0-9]+', text)
    else:
        tokens = re.findall(r'[A-Za-z]+', text)

    entities = []
    for token in tokens:
        if len(token) < min_length:
            continue

        token_lower = token.lower()

        if token_lower in skip:
            continue

        if bio_only:
            if token_lower in _BIOLOGICAL_TERMS:
                entities.append(token)
            elif include_numbers and re.match(r'^[A-Z]+[0-9]+', token):
                entities.append(token)
        else:
            entities.append(token)

    if not entities:
        return text

    return ' '.join(entities)
