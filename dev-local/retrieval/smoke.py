"""Small synthetic storage probe, run inside the private development network.

verify never recreates or inserts data. Run it again after container recreation.
Project filters exercise DB behavior, not application authorization.
"""
import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request

ES = 'http://elasticsearch:9200'
MILVUS = 'http://milvus:19530'
NAME = 'megmeet_local_retrieval_probe_20260917'
AUTH = 'Basic ' + base64.b64encode(b'elastic:local-synthetic-search-only').decode()
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def request(base, path, body=None, method=None, auth=True):
    headers = {'Content-Type': 'application/json'}
    if base == ES and auth:
        headers['Authorization'] = AUTH
    req = urllib.request.Request(base + path, headers=headers,
        data=None if body is None else json.dumps(body).encode(),
        method=method or ('GET' if body is None else 'POST'))
    try:
        with HTTP.open(req, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP_{exc.code}:{path}') from None
    require(isinstance(data, dict), 'NON_OBJECT:' + path)
    if base == MILVUS:
        require(type(data.get('code')) is int and data['code'] == 0,
                f'MILVUS_CODE_{data.get("code", "missing")}:{path}')
    return data


def mv(operation, extra=None):
    return request(MILVUS, '/v2/vectordb/' + operation,
                   {'collectionName': NAME, 'dbName': 'default', **(extra or {})})


def seed():
    # Refuse to overwrite a pre-existing test namespace.
    require(not mv('collections/has')['data']['has'], 'PROBE_COLLECTION_EXISTS')
    request(ES, '/' + NAME, {
        'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
        'mappings': {'_meta': {'scope': 'local-synthetic-retrieval-probe'},
                     'properties': {'project_key': {'type': 'keyword'},
                                    'text': {'type': 'text'}}},
    }, method='PUT')
    mv('collections/create', {
        'schema': {'autoID': False, 'enableDynamicField': False, 'fields': [
            {'fieldName': 'id', 'dataType': 'Int64', 'isPrimary': True},
            {'fieldName': 'vector', 'dataType': 'FloatVector', 'elementTypeParams': {'dim': '4'}},
            {'fieldName': 'project_key', 'dataType': 'VarChar', 'elementTypeParams': {'max_length': '64'}},
        ]},
        'indexParams': [{'fieldName': 'vector', 'indexName': 'vector_idx',
                         'metricType': 'COSINE', 'params': {'index_type': 'FLAT'}}],
        'params': {'shardsNum': 1, 'consistencyLevel': 'Strong'},
    })
    records = [
        {'id': 1, 'project_key': 'synthetic-a', 'vector': [1.0, 0.0, 0.0, 0.0]},
        {'id': 2, 'project_key': 'synthetic-b', 'vector': [1.0, 0.0, 0.0, 0.0]},
        {'id': 3, 'project_key': 'synthetic-a', 'vector': [0.0, 1.0, 0.0, 0.0]},
    ]
    for row in records:
        result = request(ES, f'/{NAME}/_doc/{row["id"]}?refresh=wait_for', {
            'project_key': row['project_key'],
            'text': 'alpha calibration procedure' if row['id'] != 3 else 'beta maintenance manual',
        }, method='PUT')
        require(result.get('result') == 'created', 'ES_INSERT_NOT_CREATED')
    result = mv('entities/insert', {'data': records})
    require(result.get('data', {}).get('insertCount') == 3, 'MILVUS_INSERT_COUNT')
    mv('collections/flush')
    request(ES, f'/{NAME}/_flush', method='POST')
    mv('collections/load')
    print('SYNTHETIC_SEED: PASS', flush=True)


def verify_once():
    require(request(ES, '/')['version']['number'] == '8.19.21', 'ES_VERSION')
    try:
        request(ES, '/', auth=False)
    except RuntimeError as exc:
        require(str(exc).startswith('HTTP_401:'), 'ES_ANONYMOUS_WRONG_STATUS')
    else:
        raise RuntimeError('ES_ANONYMOUS_NOT_DENIED')
    require(request(ES, f'/{NAME}/_count')['count'] == 3, 'ES_PERSISTED_COUNT')
    for project, expected in [('synthetic-a', ['1']), ('synthetic-b', ['2']), ('synthetic-none', [])]:
        result = request(ES, f'/{NAME}/_search', {'query': {'bool': {
            'must': [{'match': {'text': 'calibration'}}],
            'filter': [{'term': {'project_key': project}}],
        }}})
        require(not result.get('timed_out'), 'ES_QUERY_TIMEOUT')
        require(result.get('_shards', {}).get('failed') == 0, 'ES_SHARD_FAILURE')
        require([hit['_id'] for hit in result['hits']['hits']] == expected, 'ES_FILTER_RESULT')
    for project, expected in [('synthetic-a', [1, 3]), ('synthetic-b', [2]), ('synthetic-none', [])]:
        result = mv('entities/search', {
            'data': [[1.0, 0.0, 0.0, 0.0]], 'annsField': 'vector', 'limit': 3,
            'filter': f'project_key == "{project}"', 'outputFields': ['project_key'],
            'consistencyLevel': 'Strong',
        })
        rows = result['data']
        require([int(row['id']) for row in rows] == expected, 'MILVUS_VECTOR_FILTER_RESULT')
        require(all(row['project_key'] == project for row in rows), 'MILVUS_FOREIGN_PROJECT_RESULT')


def verify():
    deadline = time.monotonic() + 90
    while True:
        try:
            verify_once()
            break
        except (RuntimeError, urllib.error.URLError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError('VERIFY_DEADLINE:' + str(exc)) from None
            time.sleep(2)
    print('ES_AUTH_TEXT_SEARCH_AND_PROJECT_FILTER: PASS')
    print('MILVUS_VECTOR_SEARCH_AND_PROJECT_FILTER: PASS')
    print('READ_EXISTING_DATA_ONLY: PASS')
    print('APPLICATION_ACL_AND_REAL_EMBEDDINGS: NOT_TESTED')


def cleanup():
    # Only this script's fixed, explicitly synthetic collection/index.
    mv('collections/drop')
    request(ES, '/' + NAME, method='DELETE')
    print('SYNTHETIC_PROBE_CLEANUP: PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['seed', 'verify', 'cleanup'])
    args = parser.parse_args()
    try:
        if args.phase == 'seed':
            seed()
            verify()
        elif args.phase == 'verify':
            verify()
        else:
            cleanup()
    except Exception as exc:
        print('RETRIEVAL_PROBE: FAIL', type(exc).__name__, str(exc)[:220], file=sys.stderr)
        raise SystemExit(1)
