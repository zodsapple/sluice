#!/usr/bin/env python3
"""Score a canonical ETL CSV against one declared reference scope.

Python standard library only. Originals are never edited. This checks extraction,
mapping and preservation choices within the contract, not a full-workbook ETL run.
"""
import argparse
import csv
import json
from collections import Counter
from decimal import Decimal, DecimalException, InvalidOperation, Underflow, localcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        names = reader.fieldnames or []
        if not names or '' in names or len(names) != len(set(names)):
            raise ValueError('CSV must have a nonempty, unique header row')
        rows = list(reader)
    if any(None in r or any(v is None for v in r.values()) for r in rows):
        raise ValueError('CSV row width differs from the header')
    return names, rows


def finite_decimal(value):
    if len(value) > 1024:
        raise InvalidOperation('Numeric text is outside the supported range')
    result = Decimal(value)
    if not result.is_finite():
        raise InvalidOperation('NaN and Infinity are not valid reported values')
    parts = result.as_tuple()
    if len(parts.digits) > 128 or abs(parts.exponent) > 256:
        raise InvalidOperation('Numeric precision/exponent is outside the supported range')
    return result


def canonical_decimal_key(value):
    # Exact numeric identity, never tolerance-based identity. Blank remains null.
    if value == '':
        return ''
    number = finite_decimal(value)
    if number == 0:
        return '0'
    result = format(number, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def numeric_difference(left, right):
    with localcontext() as context:
        context.prec = 1024
        context.traps[Underflow] = True
        return finite_decimal(left) - finite_decimal(right)


def compare(spec, actual_path, root=None):
    root = ROOT if root is None else Path(root)
    expected_names, expected = read_csv(root / spec['expected'])
    actual_names, actual = read_csv(actual_path)
    required = list(dict.fromkeys(spec['key'] + spec['compare_fields']))
    missing_columns = sorted(set(required) - set(actual_names))
    if missing_columns:
        return {'passed': False, 'error': 'missing_columns', 'missing_columns': missing_columns,
                'expected_rows': len(expected), 'actual_rows': len(actual)}
    if not set(required).issubset(expected_names):
        raise ValueError('Reference specification mentions missing expected columns')
    numeric = spec.get('numeric_fields', {})

    def key(row):
        return tuple(canonical_decimal_key(row[n]) if n in numeric else row[n] for n in spec['key'])

    expected_keys = [key(r) for r in expected]
    if len(expected_keys) != len(set(expected_keys)):
        raise ValueError('Invalid reference: duplicate evaluation keys')
    try:
        actual_keys = [key(r) for r in actual]
    except (DecimalException, ValueError) as error:
        return {'passed': False, 'error': 'invalid_numeric_key', 'detail': str(error),
                'expected_rows': len(expected), 'actual_rows': len(actual)}
    actual_counts = Counter(actual_keys)
    duplicate_keys = {k for k, count in actual_counts.items() if count > 1}
    expected_by_key = dict(zip(expected_keys, expected))
    actual_by_key = {k:r for k,r in zip(actual_keys,actual) if k not in duplicate_keys}
    wanted, present = set(expected_by_key), set(actual_counts)
    missing, extra = wanted - present, present - wanted
    mismatched_rows = matched_rows = 0
    examples = []
    for k in sorted(wanted & set(actual_by_key)):
        e, a = expected_by_key[k], actual_by_key[k]
        errors = []
        for field in spec['compare_fields']:
            ev, av = e[field], a[field]
            if field in numeric:
                if ev == '' or av == '':
                    equal = ev == av
                else:
                    try:
                        equal = numeric_difference(ev,av).copy_abs() <= finite_decimal(numeric[field])
                    except (DecimalException,ValueError):
                        equal = False
            else:
                equal = ev == av
            if not equal:
                errors.append({'field':field,'expected':ev,'actual':av})
        if errors:
            mismatched_rows += 1
            if len(examples) < 8:
                examples.append({'key':dict(zip(spec['key'],k)),'differences':errors[:8]})
        else:
            matched_rows += 1

    controls = []
    for control in spec.get('sum_controls',[]):
        selected = [r for r in actual if all(r.get(f)==v for f,v in control.get('filter',{}).items())]
        try:
            values = [r[control['field']] for r in selected]
            blanks = sum(v=='' for v in values)
            if blanks and control.get('null_policy','reject') != 'ignore':
                raise ValueError('Null encountered in a control that forbids nulls')
            with localcontext() as context:
                context.prec = 1024
                context.traps[Underflow] = True
                total = sum((finite_decimal(v) for v in values if v!=''),Decimal(0))
                delta = total - finite_decimal(control['expected'])
            passed = delta.copy_abs() <= finite_decimal(control['absolute_tolerance'])
            controls.append({'name':control['name'],'rows':len(selected),'null_count':blanks,
                'actual':str(total),'expected':control['expected'],'difference':str(delta),'passed':passed})
        except (DecimalException,ValueError,KeyError) as error:
            controls.append({'name':control['name'],'passed':False,'error':str(error)})

    passed = (not missing and not extra and not duplicate_keys and not mismatched_rows
              and len(actual)==len(expected) and all(c['passed'] for c in controls))
    return {'passed':passed,'scope':spec['scope'],'expected_rows':len(expected),'actual_rows':len(actual),
        'matched_rows':matched_rows,'row_match_fraction':matched_rows/len(expected) if expected else 1,
        'missing_key_count':len(missing),'extra_key_count':len(extra),'duplicate_key_count':len(duplicate_keys),
        'duplicate_extra_rows':sum(actual_counts[k]-1 for k in duplicate_keys),
        'mismatched_rows':mismatched_rows,'mismatch_examples':examples,
        'missing_key_examples':[dict(zip(spec['key'],k)) for k in sorted(missing)[:3]],
        'extra_key_examples':[dict(zip(spec['key'],k)) for k in sorted(extra)[:3]],
        'sum_controls':controls,
        'note':'Only the declared reference scope is scored. Extra CSV columns and optional provenance coordinates are allowed. Evaluation keys may be candidate signatures or provenance; see key.json. Creating this pack did not run your ETL or LLM.'}


def main():
    manifest=json.loads((ROOT/'benchmark_manifest.json').read_text(encoding='utf-8'))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=sorted(manifest['scoring_cases']))
    parser.add_argument('--actual',type=Path)
    parser.add_argument('--list',action='store_true')
    args=parser.parse_args()
    if args.list:
        print(json.dumps({k:{'expected_rows':v['expected_rows'],'expected':v['expected'],'key':v['key']}
              for k,v in manifest['scoring_cases'].items()},ensure_ascii=False,indent=2))
        return 0
    if not args.case or args.actual is None:
        parser.error('--case and --actual are required unless --list is used')
    try:
        result=compare(manifest['scoring_cases'][args.case],args.actual)
    except (OSError,ValueError,csv.Error,DecimalException) as error:
        result={'passed':False,'error':str(error)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
