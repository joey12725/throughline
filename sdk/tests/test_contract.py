from pathlib import Path

import pytest
from throughline_sdk.contract import check_plugin
from throughline_sdk.plugin import Redactor


@pytest.mark.parametrize(
    "entry",
    ["throughline_template:Template", ["node", "plugins/template-node/plugin.mjs"]],
)
def test_plg_1_plg_2_plg_3_plg_4_plg_6_plg_7_msg_1_msg_2_msg_3_msg_4_msg_5_msg_6_contract(
    entry,
):
    source = Path("plugins/template/tests/recorded/events.jsonl").resolve()
    result = check_plugin(
        entry,
        {"TEMPLATE_INPUT": str(source), "TEMPLATE_SECRET": "fake-contract-canary"},
        secrets=["fake-contract-canary"],
    )
    assert result["messages"] == 24


def test_plg_6_core_never_imports_sdk():
    for path in Path("src/throughline").rglob("*.py"):
        assert "throughline_sdk" not in path.read_text()


def test_plg_6_redaction():
    assert (
        Redactor(["fake-secret"]).clean("failure fake-secret") == "failure [REDACTED]"
    )
