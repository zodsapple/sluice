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
