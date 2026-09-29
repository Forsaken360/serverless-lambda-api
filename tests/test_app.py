"""Unit tests for src/app.py.

boto3 is stubbed out with unittest.mock — no moto, no AWS credentials needed.
Run:  python -m pytest tests/ -v
"""

import base64
import json
import os
import sys
from decimal import Decimal
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import app

# Captured before any fixture patches app._dynamodb_table.
_REAL_TABLE_FN = app._dynamodb_table


class FakeConditionalCheckFailed(Exception):
    """Stand-in for boto3's ConditionalCheckFailedException."""


@pytest.fixture
def table(monkeypatch):
    t = mock.MagicMock()
    t.meta.client.exceptions.ConditionalCheckFailedException = FakeConditionalCheckFailed
    monkeypatch.setenv("TABLE_NAME", "links-test")
    monkeypatch.setattr(app, "_dynamodb_table", lambda: t)
    return t


def _event(body=None, path_params=None, query_params=None, headers=None):
    return {
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": path_params,
        "queryStringParameters": query_params,
        "headers": headers or {"host": "abc123.execute-api.us-east-1.amazonaws.com"},
    }


def _json(response):
    return json.loads(response["body"])


# --- create_link -----------------------------------------------------------


def test_create_link_success(table):
    resp = app.create_link(_event({"url": "https://example.com/a-very-long/path"}), None)

    assert resp["statusCode"] == 201
    body = _json(resp)
    assert len(body["code"]) == app.CODE_LENGTH
    assert body["url"] == "https://example.com/a-very-long/path"
    assert body["short_url"].endswith("/" + body["code"])

    _, kwargs = table.put_item.call_args
    assert kwargs["Item"]["code"] == body["code"]
    assert kwargs["Item"]["clicks"] == 0
    assert "created_at" in kwargs["Item"]
    assert kwargs["ConditionExpression"] == "attribute_not_exists(#c)"


def test_create_link_custom_code(table):
    resp = app.create_link(_event({"url": "https://example.com", "code": "my-link_1"}), None)

    assert resp["statusCode"] == 201
    assert _json(resp)["code"] == "my-link_1"


def test_create_link_custom_code_conflict_returns_409(table):
    table.put_item.side_effect = FakeConditionalCheckFailed("exists")

    resp = app.create_link(_event({"url": "https://example.com", "code": "taken"}), None)

    assert resp["statusCode"] == 409


def test_create_link_retries_on_generated_code_collision(table):
    table.put_item.side_effect = [FakeConditionalCheckFailed("collision"), {}]

    resp = app.create_link(_event({"url": "https://example.com"}), None)

    assert resp["statusCode"] == 201
    assert table.put_item.call_count == 2


@pytest.mark.parametrize("bad", ["not-a-url", "ftp://example.com/x", "", "   ", "https://"])
def test_create_link_rejects_bad_urls(table, bad):
    resp = app.create_link(_event({"url": bad}), None)

    assert resp["statusCode"] == 400
    assert not table.put_item.called


def test_create_link_requires_url(table):
    assert app.create_link(_event({}), None)["statusCode"] == 400
    assert not table.put_item.called


def test_create_link_rejects_bad_custom_code(table):
    resp = app.create_link(_event({"url": "https://example.com", "code": "has space!"}), None)

    assert resp["statusCode"] == 400
    assert not table.put_item.called


def test_create_link_rejects_malformed_json(table):
    event = _event()
    event["body"] = "{not json"

    assert app.create_link(event, None)["statusCode"] == 400


def test_create_link_500_when_table_not_configured(monkeypatch):
    monkeypatch.delenv("TABLE_NAME", raising=False)
    monkeypatch.setattr(app, "_dynamodb_table", _REAL_TABLE_FN)

    resp = app.create_link(_event({"url": "https://example.com"}), None)

    assert resp["statusCode"] == 500


# --- redirect --------------------------------------------------------------


def test_redirect_hit_returns_301_and_increments_clicks(table):
    table.update_item.return_value = {
        "Attributes": {"url": "https://example.com/page", "clicks": 4}
    }

    resp = app.redirect(_event(path_params={"code": "aB3xYz9"}), None)

    assert resp["statusCode"] == 301
    assert resp["headers"]["Location"] == "https://example.com/page"
    _, kwargs = table.update_item.call_args
    assert kwargs["Key"] == {"code": "aB3xYz9"}
    assert "if_not_exists(clicks" in kwargs["UpdateExpression"]


def test_redirect_miss_returns_404(table):
    table.update_item.side_effect = FakeConditionalCheckFailed("missing")

    resp = app.redirect(_event(path_params={"code": "nope"}), None)

    assert resp["statusCode"] == 404


def test_redirect_missing_code_returns_400(table):
    assert app.redirect(_event(), None)["statusCode"] == 400


# --- list_links ------------------------------------------------------------


def test_list_links(table):
    table.scan.return_value = {
        "Items": [
            {
                "code": "aaa",
                "url": "https://a.example",
                "created_at": "2026-01-01T00:00:00+00:00",
                "clicks": Decimal("3"),
            },
            {"code": "bbb", "url": "https://b.example", "created_at": "2026-01-02T00:00:00+00:00"},
        ],
        "Count": 2,
    }

    resp = app.list_links(_event(query_params={}), None)

    assert resp["statusCode"] == 200
    body = _json(resp)
    assert body["count"] == 2
    assert body["links"][0]["clicks"] == 3  # Decimal serialized cleanly
    assert body["links"][1]["clicks"] == 0  # missing clicks defaults to 0
    assert "next_token" not in body
    _, kwargs = table.scan.call_args
    assert kwargs["Limit"] == 20  # default limit


def test_list_links_paginates(table):
    table.scan.return_value = {
        "Items": [{"code": "aaa", "url": "https://a.example", "created_at": "t", "clicks": 0}],
        "Count": 1,
        "LastEvaluatedKey": {"code": "aaa"},
    }

    body = _json(app.list_links(_event(query_params={"limit": "1"}), None))

    assert "next_token" in body
    # the token round-trips back to the LastEvaluatedKey
    token = body["next_token"]
    padded = token + "=" * (-len(token) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded)) == {"code": "aaa"}
    _, kwargs = table.scan.call_args
    assert kwargs["Limit"] == 1


def test_list_links_accepts_next_token(table):
    table.scan.return_value = {"Items": [], "Count": 0}
    raw = base64.urlsafe_b64encode(json.dumps({"code": "aaa"}).encode()).decode().rstrip("=")

    resp = app.list_links(_event(query_params={"next_token": raw}), None)

    assert resp["statusCode"] == 200
    _, kwargs = table.scan.call_args
    assert kwargs["ExclusiveStartKey"] == {"code": "aaa"}


@pytest.mark.parametrize(
    "params",
    [{"limit": "0"}, {"limit": "101"}, {"limit": "many"}, {"next_token": "!!!"}],
)
def test_list_links_rejects_bad_input(table, params):
    assert app.list_links(_event(query_params=params), None)["statusCode"] == 400
