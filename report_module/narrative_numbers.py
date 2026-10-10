"""Presentation-only decimal formatting for report commentary."""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

_PROTECTED = re.compile(r'https?://\S+|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\b\d{1,4}([./-])\d{1,2}\1\d{1,4}\b|\b\d+(?:\.\d+){2,}\b|\b\d+(?:[.,]\d+)?[eE][+-]?\d+\b')
_DECIMAL = re.compile(r'(?<![\w.,])(-?\d+[.,]\d+)(?!\w|[.,]\d)')

def format_narrative_numbers(value: str, language: str = 'en') -> str:
    tr = language.lower().startswith('tr')
    protected = [(m.start(), m.end()) for m in _PROTECTED.finditer(value)]
    def replace(match):
        token = match.group()
        if any(start <= match.start() < end for start, end in protected):
            return token
        # Preserve locale-specific thousands separators rather than treating them as decimals.
        separator = '.' if tr else ','
        if re.fullmatch(r'-?[1-9]\d{0,2}' + re.escape(separator) + r'\d{3}', token):
            return token
        try:
            with localcontext() as context:
                context.prec = max(28, len(token) + 4)
                rounded = Decimal(token.replace(',', '.')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            result = format(rounded, 'f').rstrip('0').rstrip('.')
            if rounded == 0:
                result = '0'
            return result.replace('.', ',') if tr else result
        except InvalidOperation:
            return token
    return _DECIMAL.sub(replace, value)

# Only commentary fields are eligible. Numeric evidence, IDs, names and titles stay intact.
_FIELDS = {'text', 'analysis', 'overall', 'recommendation', 'summary', 'explanation',
           'positive', 'weakness', 'strategy', 'match_outlook', 'strengths', 'weaknesses',
           'conclusion', 'interpretation', 'insights', 'narrative'}

def format_narrative_fields(value, language='en', narrative=False):
    if isinstance(value, str):
        return format_narrative_numbers(value, language) if narrative else value
    if isinstance(value, list):
        return [format_narrative_fields(item, language, narrative) for item in value]
    if isinstance(value, dict):
        return {key: format_narrative_fields(item, language, key in _FIELDS or (narrative and isinstance(item, str) and key not in {'name', 'title', 'header', 'player_name', 'playerId', 'id', 'url'}))
                for key, item in value.items()}
    return value
