import hashlib
import json

import httpx
import pytest

from financial_evals.review import collect_review


def source(tmp_path):
    path = tmp_path / 'example.txt'
    path.write_text('PRIVATE ORIGINAL')
    return {'sources': [{'relative_path': path.name, 'source_id': hashlib.sha256(path.read_bytes()).hexdigest()}]}


def response(text='CAFE -5.00', **extra):
    return httpx.Response(200, json={'text': text, 'pages': 1, 'redactions': 1, **extra})


def test_review_only_contains_server_text_and_never_attests_review(tmp_path):
    inventory = source(tmp_path)
    seen = []
    def handler(request):
        seen.append(request)
        assert 'PRIVATE ORIGINAL' not in request.content.decode()
        return response()
    with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(handler)) as client:
        result = collect_review(client, tmp_path, inventory)
    assert 'PRIVATE ORIGINAL' not in json.dumps(result)
    assert result['sources'][0]['scrubbed_text'] == 'CAFE -5.00'
    assert result['sources'][0]['privacy_reviewed'] is False
    assert result['sources'][0]['source_verified'] is False
    assert [(r.method, r.url.path) for r in seen] == [('POST', '/documents/extract')]


@pytest.mark.parametrize('failure', ['changed', 'escape', 'absolute', 'empty'])
def test_invalid_inventory_refused_before_any_request(tmp_path, failure):
    inventory = source(tmp_path)
    if failure == 'changed':
        (tmp_path / 'example.txt').write_text('changed')
    elif failure == 'escape':
        inventory['sources'][0]['relative_path'] = '../outside.txt'
    elif failure == 'absolute':
        inventory['sources'][0]['relative_path'] = str(tmp_path / 'example.txt')
    else:
        inventory['sources'] = []
    def unexpected(request):
        pytest.fail('Invalid inventory must not contact API')
    with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError):
            collect_review(client, tmp_path, inventory)


@pytest.mark.parametrize('reply', [response(partial=True), response(zero_pages=[2]), response(text=''),
                                  httpx.Response(422, json={'secret': 'PRIVATE ERROR'})])
def test_refusals_do_not_become_successful_review_packets(tmp_path, reply):
    inventory = source(tmp_path)
    with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(lambda _: reply)) as client:
        with pytest.raises(ValueError) as exc:
            collect_review(client, tmp_path, inventory)
    assert 'PRIVATE ERROR' not in str(exc.value)


def test_source_mutation_during_extraction_is_detected(tmp_path):
    inventory = source(tmp_path)
    def handler(request):
        (tmp_path / 'example.txt').write_text('replaced')
        return response()
    with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match='during extraction'):
            collect_review(client, tmp_path, inventory)
