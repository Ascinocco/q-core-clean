"""Collect API-scrubbed text for LOCAL human review, never model approval.

Original files are opened only to verify their byte hashes. Their text is read
only by the server's extraction/redaction endpoint. Output is private and must
not be displayed in an agent session before the required privacy decision.
"""

import hashlib
from pathlib import Path

from api.redaction import assert_no_account_numbers
from financial_evals.core import artifact


def collect_review(client, root, inventory):
    root = Path(root).resolve()
    sources = inventory['sources']
    if not sources:
        raise ValueError('Empty source inventory')
    paths = []
    # Validate all targets before making requests; never let an inventory
    # substitute another file or escape its explicitly supplied root.
    for source in sources:
        relative = Path(source['relative_path'])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Source outside inventory root or missing')
        if hashlib.sha256(path.read_bytes()).hexdigest() != source['source_id']:
            raise ValueError('Source changed since inventory')
        paths.append(path)

    records = []
    for index, (source, path) in enumerate(zip(sources, paths), start=1):
        response = client.post('/documents/extract', json={'path': str(path)})
        if response.status_code != 200:
            # Do not copy error bodies or exception reprs into artifacts.
            raise ValueError('Server refused source extraction')
        body = response.json()
        text = body['text']
        if not text.strip() or body.get('partial') or body.get('zero_pages'):
            raise ValueError('Incomplete source extraction')
        assert_no_account_numbers(text)
        if hashlib.sha256(path.read_bytes()).hexdigest() != source['source_id']:
            raise ValueError('Source changed during extraction')
        records.append({'case_id': f'source-{index:03d}', 'source_id': source['source_id'],
                        'local_source_path': str(path), 'format': path.suffix.lower(),
                        'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'pages': body['pages'], 'redactions': body['redactions'],
                        'privacy_reviewed': False, 'source_verified': False,
                        'scrubbed_text': text})
    return artifact('local-source-review', inventory, {
        'sources': records,
        'metrics': {'documents': len(records), 'privacy_approved': 0, 'source_verified': 0},
        'instructions': 'Local human review only. Contains source paths and unreviewed scrubbed text. '
                        'Do not send this bundle to a model. An API success or a reviewed profile '
                        'does not attest document privacy or establish expected transaction rows.'})
