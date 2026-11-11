"""Conservative markers for obvious instruction attacks; not a complete detector."""
import re

_INSTRUCTIONS = re.compile(
    r'ignore\s+(?:all\s+)?(?:previous|prior|system)\s+instructions|'
    r'(?:reveal|print|send|exfiltrate)\s+(?:the\s+|your\s+|all\s+)?(?:api[ _-]?keys?|secrets?|system\s+prompt)|'
    r'<\|(?:system|im_start)\|>|\[INST\]', re.I)


def suspicious_instructions(text: str) -> bool:
    return bool(_INSTRUCTIONS.search(text))
