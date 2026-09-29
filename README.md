# serverless-lambda-api

A serverless URL shortener: three Python 3.12 Lambda functions behind an API
Gateway HTTP API, with DynamoDB as the store. All infrastructure is defined
with AWS SAM in `template.yaml` — no console click-ops.

## Architecture

A client hits the HTTP API. API Gateway routes by path and method to one of
three Lambda functions, each with least-privilege IAM scoped to the DynamoDB
table via the SAM `DynamoDBCrudPolicy`:

- `POST /links` → `create_link`: parses the JSON body, validates the URL
  (must be http/https with a host), then mints a 7-character code from a
  cryptographically secure RNG. The code is written with a
  `attribute_not_exists` conditional write, so a collision retries with a
  fresh code instead of overwriting; a taken *custom* alias returns 409.
  Responds 201 with the code, URL, and full short URL.
- `GET /{code}` → `redirect`: runs a single `update_item` guarded by
  `attribute_exists` — a missing code becomes a 404 instead of creating a
  phantom row — which atomically increments the click counter and returns
  the target URL. Responds 301 with a `Location` header.
- `GET /links` → `list_links`: paginated `scan` with `?limit=` (1–100,
  default 20) and an opaque base64 `?next_token=` cursor.

The table uses on-demand billing (pay-per-request), so the whole stack costs
nothing at rest.

## Project structure

```
├── src/
│   ├── app.py            # the three Lambda handlers
│   └── requirements.txt  # boto3 (also present in the Lambda runtime)
├── tests/
│   ├── test_app.py       # pytest suite, boto3 stubbed with unittest.mock
│   └── requirements.txt
├── template.yaml         # SAM: HTTP API + 3 functions + DynamoDB table
└── .github/workflows/ci.yml
```

## Prerequisites

- AWS CLI v2 with credentials configured (`aws sts get-caller-identity` works)
- AWS SAM CLI (`sam --version`)
- Python 3.12 (for the test suite; Docker only needed for `sam local`)

## Run the tests

```bash
pip install -r tests/requirements.txt
python -m pytest tests/ -v
```

The suite stubs boto3 with `unittest.mock` — no AWS credentials or network
access required.

## Deploy

```bash
sam build
sam deploy --guided
```

`--guided` asks for a stack name and region once, then saves the answers to
`samconfig.toml` for repeat deploys. On success the `ApiBaseUrl` output is
your endpoint.

Try it:

```bash
API=$(aws cloudformation describe-stacks --stack-name <stack> \
  --query "Stacks[0].Outputs[?OutputKey=='ApiBaseUrl'].OutputValue" --output text)

curl -s -X POST "$API/links" \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/some/very/long/path"}'

curl -s "$API/links?limit=5"
curl -s -o /dev/null -w '%{http_code} -> %{redirect_url}\n' "$API/<code>"
```

## Cleanup

```bash
sam delete
```

This removes the API, functions, and table. (Deleting the stack deletes the
table and its data — export anything you need first.)

## What I'd add next

- **Custom domain** via Route 53 + ACM certificate on the HTTP API, so short
  links live on your own domain instead of `execute-api`.
- **TTL expiry**: a DynamoDB TTL attribute (`expires_at`) so links can
  auto-delete after N days — free cleanup, no Lambda needed.
- **Abuse controls**: API Gateway throttling / usage plans per API key, plus
  stricter URL validation (blocklist for phishing/malware hosts).
- **Analytics**: click timestamps in a sort-keyed GSI for per-link traffic
  graphs instead of a single counter.
