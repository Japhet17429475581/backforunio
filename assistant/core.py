"""Pure transformations; no network, secrets or database access."""
import hashlib
import math
import re

PIPELINE = 'unio-page-window-v1'

def normalize(text):
    return re.sub(r'\s+', ' ', text.replace('\x00', '')).strip()

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()

def chunks(pages, size=1600, overlap=240):
    """Preserve physical page/paragraph location; expand adjacent chunks on retrieval."""
    if not 0 <= overlap < size:
        raise ValueError('Invalid overlap')
    result = []
    for p in pages:
        text = normalize(p['text'])
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                boundary = text.rfind(' ', start + size // 2, end)
                if boundary > start:
                    end = boundary
            result.append({'ordinal': len(result), 'page': p.get('page'),
                           'locator': p.get('locator') or f"PDF page {p.get('page')}",
                           'text': text[start:end], 'ocr': p.get('ocr', False)})
            if end == len(text):
                break
            start = max(start + 1, end - overlap)
    return result

def query_text(text):
    text = normalize(text)
    # Expand colloquial forms for retrieval only; preserve the user's original message.
    replacements = [
        (r'\bдоп\.?[\s-]*(?=(?:образован|обучен)\w*\b)', 'дополнительное '),
        (r'\bдпо\b', 'дополнительное профессиональное образование'),
        (r'\bидо\b', 'институт дополнительного образования'),
        (r'\bиия\b', 'институт иностранных языков'),
        (r'\bиряс\b', 'институт русского языка и словесности'),
        (r'\bимо\b', 'институт международного образования'),
        (r'\bим[и]?ф\b', 'институт математики информатики и физики'),
        (r'\bсоц\.?\s+(?=стип)', 'социальная '),
        (r'\bстипух\w*\b', 'стипендия'),
        (r'\bобщаг\w*\b', 'общежитие'),
        (r'\bмат\.?\s*помощ\w*\b', 'материальная помощь'),
        (r'\bакадем\b', 'академический отпуск'),
        (r'\bпроф\.?\s*(?=переподготов)', 'профессиональная '),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text

def vector_literal(values, dim):
    if len(values) != dim or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in values):
        raise ValueError('Invalid embedding dimension or values')
    if sum(x*x for x in values) == 0:
        raise ValueError('Zero embedding')
    return '[' + ','.join(str(float(x)) for x in values) + ']'

def fuse(dense, lexical, k=60):
    scores, rows = {}, {}
    for items in (dense, lexical):
        for rank, row in enumerate(items, 1):
            key = row['id']; rows[key] = row
            scores[key] = scores.get(key, 0) + 1 / (k + rank)
    return [dict(rows[key], rrf=score) for key, score in sorted(scores.items(), key=lambda x: (-x[1], x[0]))]

def validate_claims(payload, sources):
    """Verify provenance, not semantic entailment. Fail closed on invalid output."""
    known = {x['source_id']: x for x in sources}
    if payload.get('status') != 'answered':
        return None
    claims = payload.get('claims')
    if not isinstance(claims, list) or not 1 <= len(claims) <= 12:
        return None
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get('text'), str) or not 1 <= len(claim['text']) <= 1800:
            return None
        evidence = claim.get('evidence')
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 5:
            return None
        for ev in evidence:
            if not isinstance(ev, dict):
                return None
            src = known.get(ev.get('source_id'))
            quote = normalize(ev.get('quote', '')) if isinstance(ev.get('quote'), str) else ''
            if src is None or len(quote) < 15 or quote not in normalize(src['text']):
                return None
    return claims
