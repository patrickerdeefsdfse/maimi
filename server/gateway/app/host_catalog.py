"""Closed adapter from the public model list to the internal Host catalog.

Public OpenAI presentation fields are checked and omitted. This module is pure:
it does not fetch a catalog, authorize models, or create a Host.
"""
import json
import re


MAX_MODELS = 1000
MAX_CATALOG_BYTES = 524288
_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}')


class HostCatalogRefused(ValueError):
    """Only a fixed code; catalog contents never appear in an exception."""


def _need(condition):
    if not condition:
        raise HostCatalogRefused('personal_cloud_catalog_invalid')


def manager_catalog(value):
    """Return a fresh canonical catalog accepted by the existing bootstrap.

Accept exactly the public broker shape or the existing internal shape. Unknown
fields are rejected instead of silently being discarded or sent to a worker.
"""
    _need(type(value) is dict)
    public = 'object' in value
    _need(set(value) == ({'object', 'data', 'default_model'} if public
                         else {'data', 'default_model'}))
    _need(not public or value['object'] == 'list')
    items = value['data']
    _need(type(items) is list and len(items) <= MAX_MODELS)
    result = []
    names = set()
    for item in items:
        _need(type(item) is dict)
        allowed = {'id', 'name', 'inputModalities'}
        if public:
            allowed |= {'object', 'owned_by'}
            _need(item.get('object') == 'model' and item.get('owned_by') == 'megmeet')
        _need(set(item) <= allowed)
        ident = item.get('id')
        _need(type(ident) is str and _MODEL_ID.fullmatch(ident) is not None and ident not in names)
        names.add(ident)
        _need(item.get('name') is None or type(item['name']) is str and len(item['name']) <= 500)
        normalized = {'id': ident}
        if 'name' in item:
            normalized['name'] = item['name']
        if 'inputModalities' in item:
            modes = item['inputModalities']
            _need(type(modes) is list and 1 <= len(modes) <= 2 and
                  all(type(mode) is str and mode in ('text', 'image') for mode in modes) and
                  'text' in modes and len(set(modes)) == len(modes))
            normalized['inputModalities'] = list(modes)
        result.append(normalized)
    default = value['default_model']
    _need((type(default) is str and default in names) if names else default is None)
    normalized = {'data': result, 'default_model': default}
    try:
        encoded = json.dumps(normalized, ensure_ascii=True, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('ascii')
    except (TypeError, ValueError, UnicodeError):
        raise HostCatalogRefused('personal_cloud_catalog_invalid') from None
    _need(0 < len(encoded) <= MAX_CATALOG_BYTES)
    return normalized
