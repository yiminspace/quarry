from __future__ import annotations

import json
from urllib.parse import urlencode

from quarry.neptune_empty import parse_request


def test_parse_boto3_json_request() -> None:
    query, params = parse_request(
        "application/json",
        json.dumps({"query": "MATCH (n) RETURN n", "parameters": '{"user_id":5527}'}).encode(),
    )
    assert query == "MATCH (n) RETURN n"
    assert params == {"user_id": 5527}


def test_parse_quarry_form_request() -> None:
    query, params = parse_request(
        "application/x-www-form-urlencoded",
        urlencode({"query": "RETURN 1", "parameters": '{"x":1}'}).encode(),
    )
    assert query == "RETURN 1"
    assert params == {"x": 1}


def test_parse_missing_parameters() -> None:
    query, params = parse_request("application/json", b'{"openCypherQuery":"RETURN 1"}')
    assert query == "RETURN 1"
    assert params == {}
