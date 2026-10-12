"""The browser bridge uses the same engine, writes only validated output, and refuses unsupported specs."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def browser(tmp_path):
    spec = importlib.util.spec_from_file_location("browser_adapter", ROOT / "sites/playground/runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SESSION = tmp_path
    module.reset_session()
    for name in ("input.csv", "template.csv"):
        shutil.copyfile(ROOT / "sluice/examples/quickstart" / name, tmp_path / name)
    return module


def request(**overrides):
    return {"action": "preview", "input": "input.csv", "template": "template.csv",
            "spec": (ROOT / "sluice/examples/quickstart/spec.yaml").read_text(), **overrides}


def test_browser_preview_convert_and_failure(browser):
    preview = json.loads(browser.execute(json.dumps(request())))
    assert preview["ok"] and preview["output"] is None and len(preview["outputPreview"]["rows"]) == 3
    converted = json.loads(browser.execute(json.dumps(request(action="convert"))))
    assert converted["ok"] and Path(converted["output"]).exists()
    source = browser.SESSION / "input.csv"
    source.write_text(source.read_text().replace("100.125", "100.129"))
    failed = json.loads(browser.execute(json.dumps(request())))
    assert not failed["ok"] and failed["output"] is None


def test_browser_transport_template_and_generated_filename(browser):
    fix = ROOT / "sluice/tests/fixtures/transport"
    shutil.copyfile(fix / "input/DHL_DE_202604_Transport.xlsx", browser.SESSION / "input.xlsx")
    shutil.copyfile(fix / "transport_template.xlsx", browser.SESSION / "template.xlsx")
    spec = (ROOT / "sluice/specs/dhl_de_transport.yaml").read_text()
    result = json.loads(browser.execute(json.dumps(request(action="convert", input="input.xlsx", template="template.xlsx", spec=spec))))
    assert result["ok"] and result["output"].endswith("CTD_EMEA_TRANSPORT_DE_DHL_202604.xlsx")


def test_browser_schema_drafting(browser):
    schema = (ROOT / "sluice/examples/quickstart/schema.json").read_text()
    draft = json.loads(browser.execute(json.dumps(request(action="draft", schema=schema))))
    assert "declared_total" in draft["spec"] and not draft["questions"]


@pytest.mark.parametrize("action", ["preview", "draft"])
def test_browser_large_input_uses_local_cli(browser, action):
    (browser.SESSION / "input.csv").write_text("delivery_id\n" + "A001\n" * 20001)
    schema = (ROOT / "sluice/examples/quickstart/schema.json").read_text()
    with pytest.raises(ValueError, match="20,000"):
        browser.execute(json.dumps(request(action=action, schema=schema)))


@pytest.mark.parametrize("fragment", ["shape: [{op: hook, python: 'os:system'}]", "sources: [{name: other, file: '/private.xlsx', map: {}}]"])
def test_browser_disables_hooks_and_external_sources(browser, fragment):
    with pytest.raises(ValueError):
        browser.safe_spec(request()["spec"] + "\n" + fragment)


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_browser_drafts_from_target_without_json(browser, extension):
    import pandas as pd
    target = pd.DataFrame({'delivery_id': ['A001', 'A002', 'A003'], 'amount': [100.125, 50.25, 15.125]})
    path = browser.SESSION / f'expected.{extension}'
    if extension == 'xlsx':
        target.to_excel(path, index=False)
    else:
        target.to_csv(path, index=False)
    draft = json.loads(browser.execute(json.dumps(request(action='draft', schema='', template=path.name,
                                                          targetRole='expected'))))
    assert 'delivery_id:' in draft['spec'] and 'amount:' in draft['spec']
    assert 'complete expected result' in draft['review']
    preview = json.loads(browser.execute(json.dumps(request(spec=draft['spec'], template=path.name,
                                                            targetRole='expected'))))
    assert preview['ok'], preview['report']['diagnosis']
    assert preview['report']['controls'][-1]['kind'] == 'expected_rows'


def test_expected_checks_values_duplicates_and_never_writes_failed_output(browser):
    import pandas as pd
    reference = json.loads(browser.execute(json.dumps(request())))['outputPreview']
    frame = pd.DataFrame(reference['rows'], columns=reference['columns'])
    frame.iloc[::-1].to_csv(browser.SESSION / 'expected.csv', index=False)
    req = request(action='convert', template='expected.csv', targetRole='expected')
    good = json.loads(browser.execute(json.dumps(req)))
    assert good['ok'] and Path(good['output']).exists()
    # A different ID with identical row counts and money totals must still fail.
    frame.iloc[0, 0] = 'WRONG'
    frame.to_csv(browser.SESSION / 'expected.csv', index=False)
    existing = {p: p.read_bytes() for p in (browser.SESSION / 'out').glob('*.csv')}
    bad = json.loads(browser.execute(json.dumps(req)))
    assert not bad['ok'] and bad['output'] is None
    assert bad['report']['diagnosis']['code'] == 'expected_mismatch'
    assert {p: p.read_bytes() for p in (browser.SESSION / 'out').glob('*.csv')} == existing
    duplicate = pd.DataFrame({'id': ['001', '001'], 'amount': ['1.00', '2.00']})
    assert not browser.expected_control(duplicate, duplicate.iloc[[0, 0]])['passed']
    assert browser.expected_control(duplicate, duplicate.iloc[::-1])['passed']
    assert not browser.expected_control(duplicate, duplicate.assign(id=['1', '1']))['passed']


def test_target_headers_and_header_only_template(browser):
    (browser.SESSION / 'target.csv').write_text('id,id\n1,2\n')
    with pytest.raises(ValueError, match='unique'):
        browser.execute(json.dumps(request(action='draft', template='target.csv', schema='')))
    (browser.SESSION / 'target.csv').write_text('delivery_id,amount\n')
    with pytest.raises(ValueError, match='needs data rows'):
        browser.execute(json.dumps(request(action='draft', template='target.csv', schema='', targetRole='expected')))
    draft = json.loads(browser.execute(json.dumps(request(action='draft', template='target.csv', schema='', targetRole='template'))))
    assert 'delivery_id:' in draft['spec']


def test_expected_sheet_provenance_limits_draft_scope(browser):
    import pandas as pd
    import yaml
    with pd.ExcelWriter(browser.SESSION / 'input.xlsx') as writer:
        pd.DataFrame({'id': ['001'], 'amount': [1]}).to_excel(writer, sheet_name='Data', index=False)
        pd.DataFrame({'notes': ['Do not extract me']}).to_excel(writer, sheet_name='Notes', index=False)
    (browser.SESSION / 'expected.csv').write_text('id,amount,source_sheet\n001,1,Data\n')
    draft = json.loads(browser.execute(json.dumps(request(action='draft', input='input.xlsx', template='expected.csv',
                                                          targetRole='expected', schema=''))))
    spec = yaml.safe_load(draft['spec'])
    assert spec['locate']['sheet']['name'] == 'Data' and not spec['sources']
    assert 'Draft source sheets: Data' in draft['review']


def decision_draft(browser, extra_fields=('currency',)):
    import pandas as pd
    schema = json.loads((ROOT / 'sluice/examples/quickstart/schema.json').read_text())
    frame = pd.read_csv(browser.SESSION / 'input.csv', dtype=str)
    for name in extra_fields:
        schema['fields'].append({'name': name, 'type': 'string', 'nullable': False})
        schema['example_row'][name] = 'EUR' if name == 'currency' else 'confirmed'
        frame[name] = schema['example_row'][name]
    frame.to_csv(browser.SESSION / 'expected.csv', index=False)
    req = request(action='draft', template='expected.csv', targetRole='expected', schema=json.dumps(schema))
    return req, json.loads(browser.execute(json.dumps(req)))


def reply_for(question, value, confidence=1):
    return {'question_id': question['id'], 'status': 'answered', 'bindings': [
        {'kind': 'constant', 'target': question['targets'][0], 'args': {'value': value}, 'confidence': confidence}]}


def test_json_config_prompt_fenced_reply_and_validated_conversion(browser):
    req, draft = decision_draft(browser)
    assert draft['config']['files']['input']['sha256'] and len(draft['questions']) == 1
    prompt = draft['promptPackets'][0]
    assert '"answers"' in prompt['text'] and 'example_row' in prompt['text'] and 'EUR' in prompt['text']
    assert prompt['reply']['answers'][0]['status'] == 'unknown'
    reply = '```json\n' + json.dumps({'answers': [reply_for(draft['questions'][0], 'EUR')]}) + '\n```'
    resolved = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'reply': reply})))
    assert not resolved['questions'] and not resolved['prompts']
    assert resolved['config']['spec']['template']['columns']['currency']['col'] == 'currency'
    result = json.loads(browser.execute(json.dumps({**req, 'action': 'convert', 'config': resolved['config']})))
    assert result['ok'] and Path(result['output']).exists()
    controls = {c['name']: c['passed'] for c in result['report']['controls']}
    assert controls['configuration_questions'] and controls['expected_result']


def test_partial_reply_manual_entry_and_config_roundtrip(browser):
    req, draft = decision_draft(browser, ('currency', 'review_status'))
    q = {x['targets'][0]: x for x in draft['questions']}
    partial = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'],
                         'reply': json.dumps({'answers': [reply_for(q['currency'], 'EUR')]})})))
    assert len(partial['questions']) == 1 and partial['questions'][0]['targets'] == ['review_status']
    complete = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': partial['config'], 'manual': True,
                          'reply': json.dumps({'answers': [reply_for(q['review_status'], 'confirmed', .1)]})})))
    assert not complete['questions']
    assert complete['config']['answers'][q['review_status']['id']]['bindings'][0]['origin'] == 'human'
    # Round-trip after a worker filesystem reset; no hidden worker state is needed.
    original = {f.name: f.read_bytes() for f in browser.SESSION.glob('*.csv')}
    browser.reset_session()
    for name, data in original.items():
        (browser.SESSION / name).write_bytes(data)
    restored = json.loads(browser.execute(json.dumps({**req, 'action': 'load_config', 'config': complete['config']})))
    assert restored['config'] == complete['config'] and not restored['questions']


@pytest.mark.parametrize('mode', ['unknown', 'low_confidence'])
def test_unknown_or_low_confidence_needs_manual_decision(browser, mode):
    req, draft = decision_draft(browser)
    q = draft['questions'][0]
    answer = {'question_id': q['id'], 'status': 'unknown', 'bindings': []} if mode == 'unknown' else reply_for(q, 'EUR', .2)
    result = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'],
                       'reply': json.dumps({'answers': [answer]})})))
    assert len(result['questions']) == 1 and result['notices'] and not result['config']['answers']
    with pytest.raises(ValueError, match='open question'):
        browser.execute(json.dumps({**req, 'action': 'convert', 'config': result['config']}))
    assert not list((browser.SESSION / 'out').glob('*.csv'))


@pytest.mark.parametrize('mode', ['bad_json', 'foreign_question', 'wrong_target', 'unknown_source', 'duplicate'])
def test_bad_reply_is_rejected_atomically(browser, mode):
    req, draft = decision_draft(browser)
    original = json.dumps(draft['config'], sort_keys=True)
    answer = reply_for(draft['questions'][0], 'EUR')
    if mode == 'foreign_question':
        answer['question_id'] = 'another-file:currency'
    if mode == 'wrong_target':
        answer['bindings'][0]['target'] = 'amount'
    if mode == 'unknown_source':
        answer['bindings'][0] = {'kind': 'column', 'target': 'currency', 'args': {'source': 'nonexistent'}}
    reply = 'not JSON' if mode == 'bad_json' else json.dumps({'answers': [answer, answer] if mode == 'duplicate' else [answer]})
    with pytest.raises(ValueError, match='JSON|question|target|source|Duplicate'):
        browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'reply': reply}))
    assert json.dumps(draft['config'], sort_keys=True) == original


def test_config_hash_and_expected_gate_survive_valid_answers(browser):
    req, draft = decision_draft(browser)
    wrong = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'manual': True,
                      'reply': json.dumps({'answers': [reply_for(draft['questions'][0], 'USD')]})})))
    failed = json.loads(browser.execute(json.dumps({**req, 'action': 'convert', 'config': wrong['config']})))
    assert not failed['ok'] and failed['output'] is None and failed['report']['diagnosis']['code'] == 'expected_mismatch'
    path = browser.SESSION / 'input.csv'
    path.write_text(path.read_text().replace('A001', 'A999'))
    with pytest.raises(ValueError, match='different input'):
        browser.execute(json.dumps({**req, 'action': 'load_config', 'config': wrong['config']}))


def test_reply_field_dependencies_work_in_reverse_order(browser):
    import pandas as pd
    req, _ = decision_draft(browser, ('currency', 'review_status'))
    schema = json.loads(req['schema'])
    schema['example_row']['review_status'] = 'EUR'
    req['schema'] = json.dumps(schema)
    path = browser.SESSION / 'expected.csv'
    frame = pd.read_csv(path, dtype=str)
    frame['review_status'] = 'EUR'
    frame.to_csv(path, index=False)
    draft = json.loads(browser.execute(json.dumps(req)))
    questions = {q['targets'][0]: q for q in draft['questions']}
    dependent = {'question_id': questions['review_status']['id'], 'status': 'answered', 'bindings': [
        {'kind': 'template', 'target': 'review_status', 'args': {'template': '{currency}'}, 'confidence': .95}]}
    reply = {'answers': [dependent, reply_for(questions['currency'], 'EUR')]}
    resolved = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'reply': json.dumps(reply)})))
    assert not resolved['questions']
    converted = json.loads(browser.execute(json.dumps({**req, 'action': 'convert', 'config': resolved['config']})))
    assert converted['ok'] and all(row[-2:] == ['EUR', 'EUR'] for row in converted['outputPreview']['rows'])


def test_imported_unknown_binding_cannot_silently_close_a_question(browser):
    req, draft = decision_draft(browser)
    q = draft['questions'][0]
    draft['config']['answers'][q['id']] = {'sheet': q['frame'], 'status': 'answered', 'bindings': [
        {'kind': 'unknown', 'target': 'currency', 'args': {'reason': 'not decided'}, 'origin': 'human'}]}
    with pytest.raises(ValueError, match='unknown decisions must remain open'):
        browser.execute(json.dumps({**req, 'action': 'load_config', 'config': draft['config']}))
