"""Explicit applicability checks against caller-authored fields, never source truth.

No query parsing, identity resolution, publication/import dates, inferred units or
implicit timezones. A mismatch may inform navigation ranking; callers must retain
baseline partial evidence and report unknown fields independently.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime

_SCOPE = ('Exact requested attributes and authored validity only; not source verification, '
          'compatibility inference, or permission to discard baseline partial evidence.')
_TIME = re.compile(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?')


def _time(value):
    if type(value) is not str or len(value) > 40 or not _TIME.fullmatch(value):
        raise ValueError('Use an ISO calendar date or timestamp, at most 40 characters')
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if 'T' in value else date.fromisoformat(value)


def _primitive(value):
    if value is None or type(value) is bool:
        return True
    if type(value) is str:
        return len(value) <= 500 and '\x00' not in value and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)
    if type(value) in (int, float):
        return abs(value) <= 1e15 and math.isfinite(value)
    return False


def validate_constraints(constraints):
    """Return a detached bounded request, or {} when no request was supplied.

    as_of permits ISO YYYY-MM-DD or an explicit ISO timestamp. Timestamp precision
    and timezone conventions must agree with authored bounds; we never invent them.
    """
    if constraints is None:
        return {}
    if type(constraints) is not dict or len(constraints) > 2 or constraints.keys() - {'as_of', 'attributes'}:
        raise ValueError('constraints supports only as_of and attributes')
    result = {}
    if 'as_of' in constraints:
        _time(constraints['as_of'])
        result['as_of'] = constraints['as_of']
    if 'attributes' in constraints:
        attrs = constraints['attributes']
        if type(attrs) is not dict or not 1 <= len(attrs) <= 8:
            raise ValueError('attributes must contain 1 to 8 exact primitive fields')
        for key, value in attrs.items():
            if (type(key) is not str or not key.strip() or len(key) > 80 or '\x00' in key
                    or any(0xD800 <= ord(c) <= 0xDFFF for c in key) or not _primitive(value)):
                raise ValueError('Invalid bounded attribute key or primitive value')
        result['attributes'] = dict(attrs)
    return result


def _comparable(left, right):
    if type(left) is not type(right):
        return 'time_precision_not_comparable'
    if type(left) is datetime and ((left.utcoffset() is None) != (right.utcoffset() is None)):
        return 'timezone_not_comparable'
    return None


def _validity(record, as_of):
    requested = _time(as_of)
    parsed = {}
    for field in ('valid_from', 'valid_to'):
        if field in record:
            try:
                parsed[field] = _time(record[field])
            except ValueError:
                pass
    invalid_interval = (len(parsed) == 2 and not _comparable(parsed['valid_from'], parsed['valid_to'])
                        and parsed['valid_from'] > parsed['valid_to'])
    checks = []
    for field in ('valid_from', 'valid_to'):
        check = {'field': field, 'requested_as_of': as_of, 'status': 'unknown'}
        if field not in record:
            check['reason'] = 'missing_authored_field'
        elif field not in parsed:
            check['reason'] = 'invalid_authored_date'
        else:
            check['actual'] = record[field]
            incompatible = _comparable(requested, parsed[field])
            if invalid_interval:
                check['reason'] = 'invalid_authored_interval'
            elif incompatible:
                check['reason'] = incompatible
            else:
                match = requested >= parsed[field] if field == 'valid_from' else requested <= parsed[field]
                check.update(status='match' if match else 'mismatch',
                             reason='within_inclusive_bound' if match else 'outside_inclusive_bound')
        checks.append(check)
    return checks


def evaluate_constraints(record, constraints=None):
    """Return match/mismatch/unknown and exact, individually inspectable checks.

    Missing either validity bound stays unknown, even when the other bound matches.
    A known mismatch dominates aggregate status without hiding unknown fields.
    Equal attributes require identical primitive types; bool True never equals 1.
    Caller-authored review/assertion statuses are retained, never upgraded by a match.
    """
    if type(record) is not dict:
        raise ValueError('record must be an authored record object')
    request = validate_constraints(constraints)
    checks = []
    if request and 'as_of' in request:
        checks.extend(_validity(record, request['as_of']))
    attrs = record.get('attributes')
    for key, requested in (request or {}).get('attributes', {}).items():
        check = {'field': 'attributes.' + key, 'requested': requested, 'status': 'unknown'}
        if type(attrs) is not dict or key not in attrs:
            check['reason'] = 'missing_authored_field'
        elif not _primitive(attrs[key]):
            check['reason'] = 'invalid_authored_value'
        else:
            actual = attrs[key]
            match = type(actual) is type(requested) and actual == requested
            check.update(actual=actual, status='match' if match else 'mismatch',
                         reason='exact_typed_value' if match else 'different_typed_value')
        checks.append(check)
    states = {check['status'] for check in checks}
    status = 'mismatch' if 'mismatch' in states else 'unknown' if not checks or 'unknown' in states else 'match'
    result = {'status': status, 'checks': checks,
              'missing_fields': [check['field'] for check in checks if check['reason'] == 'missing_authored_field'],
              'scope': _SCOPE}
    if not checks:
        result['reason'] = 'constraints_not_requested'
    for field in ('review_status', 'assertion_status'):
        if field in record and type(record[field]) is str and len(record[field]) <= 40:
            result[field] = record[field]
    return result
