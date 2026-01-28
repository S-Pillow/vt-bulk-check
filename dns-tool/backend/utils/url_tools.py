import re

# Sanitizing rules
SANITIZE_RULES = [
    (r'\s+', ''),  # Remove internal spaces
    (r'^https?', lambda m: 'hXXps' if m.group(0).lower() == 'https' else 'hXXp'),
    (r'\.', '[.]'),
]

# Unsanitizing rules
UNSANITIZE_RULES = [
    (r'\s+', ''),
    (r'^hXXps?', lambda m: 'https' if m.group(0).lower() == 'hxxps' else 'http'),
    (r'\[\.\]', '.'),
    (r'\[://\]', '://'),
]

def apply_rules(text, rules):
    for pattern, repl in rules:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE) if not callable(repl) else re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return text

def ensure_protocol(text, sanitize=True):
    if re.match(r'^(https?|hxxps?)://', text, flags=re.IGNORECASE):
        return text
    return ('hXXp://' if sanitize else 'http://') + text

def sanitize_urls(urls):
    return [apply_rules(ensure_protocol(url.strip(), sanitize=True), SANITIZE_RULES) for url in urls if url.strip()]

def unsanitize_urls(urls):
    return [apply_rules(ensure_protocol(url.strip(), sanitize=False), UNSANITIZE_RULES) for url in urls if url.strip()]

def extract_domains(urls):
    domains = set()
    for url in urls:
        original_url = ensure_protocol(url.strip(), sanitize=False)
        if not original_url:
            continue

        match = re.match(r'^(?:https?://)?([^/]+)', original_url, flags=re.IGNORECASE)
        if match:
            full_domain = match.group(1).lower()
            domain_parts = full_domain.split('.')
            if len(domain_parts) > 2:
                domain = '.'.join(domain_parts[-2:])
            else:
                domain = full_domain
            domains.add(domain)

    return list(domains)
