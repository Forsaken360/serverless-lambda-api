"""AWS Lambda handlers for the serverless URL shortener API.

Expects API Gateway HTTP API payload format 2.0 events.

Routes (wired in template.yaml):
    POST /links  -> create_link
    GET  /links  -> list_links
    GET  /{code} -> redirect
"""

import base64
import binascii
import functools
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME_ENV = "TABLE_NAME"
CODE_LENGTH = 7
CODE_ALPHABET = string.ascii_letters + string.digits
CUSTOM_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
MAX_URL_LENGTH = 2048
MAX_CODE_ATTEMPTS = 5
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100


class _DecimalEncoder(json.JSONEncoder):
    """Encode DynamoDB Decimals as int/float so json.dumps never chokes."""

    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


def _response(status_code, body=None, headers=None):
    payload = {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **(headers or {})},
    }
    if body is not None:
        payload["body"] = json.dumps(body, cls=_DecimalEncoder)
    return payload


def _dynamodb_table():
    """Return the boto3 DynamoDB Table resource.

    boto3 ships inside the Lambda Python runtime, so it is imported lazily;
    this also keeps the pytest suite dependency-free.
    """
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - boto3 is always present on Lambda
        raise RuntimeError("boto3 is required at runtime") from exc
    table_name = os.environ.get(TABLE_NAME_ENV)
    if not table_name:
        raise RuntimeError(f"{TABLE_NAME_ENV} environment variable is not set")
    return boto3.resource("dynamodb").Table(table_name)


def _handle_errors(handler):
    """Convert unexpected exceptions into logged 500 JSON responses."""

    @functools.wraps(handler)
    def wrapper(event, context):
        try:
            return handler(event, context)
        except Exception:
            logger.exception("unhandled error in %s", handler.__name__)
            return _response(500, {"message": "internal server error"})

    return wrapper


def _validate_url(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None, "field 'url' is required"
    url = raw.strip()
    if len(url) > MAX_URL_LENGTH:
        return None, f"url must be shorter than {MAX_URL_LENGTH} characters"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "url must be a valid http(s) URL, e.g. https://example.com/page"
    return url, None


def _generate_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _short_url_for(event, code):
    host = (event.get("headers") or {}).get("host")
    return f"https://{host}/{code}" if host else None


@_handle_errors
def create_link(event, context):
    """POST /links — validate the URL, mint a code, store it.

    Body: {"url": "https://...", "code": "optional-custom-alias"}
    """
    try:
        data = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"message": "request body must be valid JSON"})
    if not isinstance(data, dict):
        return _response(400, {"message": "request body must be a JSON object"})

    url, error = _validate_url(data.get("url"))
    if error:
        return _response(400, {"message": error})

    custom = data.get("code")
    if custom is not None:
        if not isinstance(custom, str) or not CUSTOM_CODE_RE.match(custom):
            return _response(
                400,
                {"message": "custom code must be 3-32 chars: letters, digits, '-' or '_'"},
            )

    table = _dynamodb_table()
    conditional_failed = table.meta.client.exceptions.ConditionalCheckFailedException

    code = None
    for _ in range(MAX_CODE_ATTEMPTS):
        candidate = custom or _generate_code()
        item = {
            "code": candidate,
            "url": url,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "clicks": 0,
        }
        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#c)",
                ExpressionAttributeNames={"#c": "code"},
            )
        except conditional_failed:
            if custom:
                logger.info("custom code already taken: %s", custom)
                return _response(409, {"message": f"code '{custom}' is already taken"})
            logger.info("generated code collision, retrying")
            continue
        code = candidate
        break

    if code is None:
        return _response(503, {"message": "could not mint a unique code, please retry"})

    body = {"code": code, "url": url}
    short_url = _short_url_for(event, code)
    if short_url:
        body["short_url"] = short_url
    logger.info("created link %s -> %s", code, url)
    return _response(201, body)


@_handle_errors
def redirect(event, context):
    """GET /{code} — 301 to the stored URL, 404 when the code is unknown.

    The click counter is incremented atomically in the same update, and the
    attribute_exists guard turns a missing code into a 404 instead of
    silently creating a row.
    """
    code = (event.get("pathParameters") or {}).get("code")
    if not code:
        return _response(400, {"message": "missing path parameter 'code'"})

    table = _dynamodb_table()
    conditional_failed = table.meta.client.exceptions.ConditionalCheckFailedException
    try:
        result = table.update_item(
            Key={"code": code},
            UpdateExpression="SET clicks = if_not_exists(clicks, :zero) + :one",
            ConditionExpression="attribute_exists(#c)",
            ExpressionAttributeNames={"#c": "code"},
            ExpressionAttributeValues={":zero": 0, ":one": 1},
            ReturnValues="ALL_NEW",
        )
    except conditional_failed:
        return _response(404, {"message": f"no link found for code '{code}'"})

    url = result["Attributes"]["url"]
    logger.info("redirect %s -> %s", code, url)
    return {
        "statusCode": 301,
        "headers": {"Location": url, "Cache-Control": "no-store"},
        "body": "",
    }


def _decode_next_token(raw_token):
    padded = raw_token + "=" * (-len(raw_token) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


def _encode_next_token(last_key):
    raw = json.dumps(last_key, cls=_DecimalEncoder).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


@_handle_errors
def list_links(event, context):
    """GET /links — paginated scan. ?limit= (1-100), ?next_token= for pages."""
    params = event.get("queryStringParameters") or {}

    try:
        limit = int(params.get("limit", DEFAULT_LIST_LIMIT))
    except (TypeError, ValueError):
        return _response(400, {"message": "'limit' must be an integer"})
    if not 1 <= limit <= MAX_LIST_LIMIT:
        return _response(400, {"message": f"'limit' must be between 1 and {MAX_LIST_LIMIT}"})

    scan_kwargs = {"Limit": limit}
    raw_token = params.get("next_token")
    if raw_token:
        try:
            start_key = _decode_next_token(raw_token)
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return _response(400, {"message": "'next_token' is invalid"})
        if not isinstance(start_key, dict) or not start_key:
            return _response(400, {"message": "'next_token' is invalid"})
        scan_kwargs["ExclusiveStartKey"] = start_key

    table = _dynamodb_table()
    page = table.scan(**scan_kwargs)

    links = [
        {
            "code": item["code"],
            "url": item["url"],
            "created_at": item.get("created_at"),
            "clicks": item.get("clicks", 0),
        }
        for item in page.get("Items", [])
    ]
    body = {"links": links, "count": page.get("Count", len(links))}
    if "LastEvaluatedKey" in page:
        body["next_token"] = _encode_next_token(page["LastEvaluatedKey"])
    return _response(200, body)
