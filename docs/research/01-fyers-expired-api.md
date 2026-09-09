# Fyers Expired F&O Contracts Data API, implementation specification

Source documents (authoritative, on disk):

- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/24-expired-f-o-contracts-data.md`
- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/04-request-response-structure.md`
- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/06-authentication-login-flow-user-apps.md`
- `/Users/openalgo/AIBootcamp2026/Day26/fyers api docs/29-appendix.md` (symbology, used only for the metadata derivation section)

Upstream: https://myapi.fyers.in/docsv3

Everything marked "documented" below is stated in those files. Everything marked "not documented" is an explicit gap that the implementation must handle defensively or resolve by probing the live API.

---

## 1. The three endpoints at a glance

| Purpose | Method and URL |
|---|---|
| Get Expiry Dates | `GET https://api-t1.fyers.in/data/history/fno/expired/expiry-dates` |
| Get Expired Contracts | `GET https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols` |
| Get Expired F&O Data | `GET https://api-t1.fyers.in/data/history/fno/expired/historical-data` |

Base host for all three, and for auth: `https://api-t1.fyers.in`.

All three are plain HTTP GET with query-string parameters. All three require the authorization header described in section 6.

Documented call order: expiry-dates gives you an expiry date, underlying-symbols turns (underlying, expiry date) into contract symbols, historical-data turns a contract symbol into candles. This is exactly the three stage pipeline ExpiryManager needs.

Important scope limit, quoted from the docs: "This API supports expired contracts only. It does not provide historical data for active Futures and Options contracts." Live and current-series data must come from the regular Data API (section 23 of the docs), not from these endpoints.

---

## 2. Endpoint 1: Get Expiry Dates

`GET https://api-t1.fyers.in/data/history/fno/expired/expiry-dates`

Returns the Futures and Options expiry dates available for an underlying symbol inside a requested date range. Futures and Options expiry lists are returned separately.

Documented window limit: "You can request up to 366 days at a time."

### 2.1 Request parameters

| Parameter | Type | Required | Accepted values and notes |
|---|---|---|---|
| `symbol` | string | Yes | Underlying symbol in Fyers symbology. Documented examples: `NSE:NIFTY50-INDEX`, `NSE:SBIN-EQ`, `BSE:SENSEX-INDEX`. |
| `range_from` | string | Yes | Start of range. Epoch value when `date_format=0`. `yyyy-mm-dd` when `date_format=1`. |
| `range_to` | string | Yes | End of range. Epoch value when `date_format=0`. `yyyy-mm-dd` when `date_format=1`. |
| `date_format` | int | Yes | Boolean flag. `0` means the range values are epoch. `1` means the range values are `yyyy-mm-dd`. The SDK samples pass it as the string `"1"`, so the wire format is a string containing 0 or 1. |

There is no exchange parameter. The exchange is carried inside the `symbol` prefix.

### 2.2 Response envelope

| Field | Type | Meaning |
|---|---|---|
| `s` | string | `ok` or `error` |
| `code` | int | Response code, `200` on success |
| `message` | string | Message, empty string on success |
| `data.symbol` | string | Underlying symbol for which expiry dates are returned |
| `data.from_date` | string | Start of the requested range, echoed back |
| `data.to_date` | string | End of the requested range, echoed back |
| `data.expiry_dates.futures` | array of string | Futures expiry dates in the range |
| `data.expiry_dates.options` | array of string | Options expiry dates in the range |

Verbatim documented success body:

```json
{
    "code": 200,
    "data": {
        "symbol": "SBIN",
        "from_date": "2025-01-01",
        "to_date": "2025-03-31",
        "expiry_dates": {
            "futures": ["2025-01-30", "2025-02-27", "2025-03-27"],
            "options": ["2025-01-30", "2025-02-27", "2025-03-27"]
        }
    },
    "message": "",
    "s": "ok"
}
```

Note carefully: the request sent `NSE:SBIN-EQ` and the response echoed `"symbol": "SBIN"`. The response strips the exchange prefix and series suffix. Persist the request symbol as the canonical key and treat `data.symbol` as an informational echo only. Do not join on it.

Expiry dates come back as `yyyy-mm-dd` strings even in the sample where the request used `date_format=1`. Behaviour with `date_format=0` for the returned dates is not documented.

### 2.3 Verbatim Python sample

```python
from fyers_apiv3 import fyersModel

client_id = "XC4XXXXM-100"
access_token = "eyJ0eXXXXXXXX2c5-Y3RgS8wR14g"

fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token, log_path="")

data = {
    "symbol": "NSE:SBIN-EQ",
    "range_from": "2025-01-01",
    "range_to": "2025-03-31",
    "date_format": "1"
}

response = fyers.expiry_dates(data)
print(response)
print(response["data"]["expiry_dates"]["futures"])
print(response["data"]["expiry_dates"]["options"])
```

Verbatim cURL, which shows the exact query string shape and auth header:

```bash
curl --location --request GET 'https://api-t1.fyers.in/data/history/fno/expired/expiry-dates?symbol=NSE:SBIN-EQ&range_from=2025-01-01&range_to=2025-03-31&date_format=1' \
 --header 'Authorization: app_id:access_token'
```

SDK method names for cross reference: Python `fyers.expiry_dates(data)`, Node and Web `fyers.get_expiry_dates(reqBody)`, C# `GetExpiryDates(symbol, from, to, dateFormat)`, Java `GetHistoryExpiryDates(...)`, Go `GetExpiryDates(ExpiryDatesRequest{UnderlyingSymbol, RangeFrom, RangeTo, DateFormat})`, C `fyers_model_get_history_expiry_dates`.

The Go SDK names the field `UnderlyingSymbol` while the wire parameter is `symbol`. Use the wire name.

---

## 3. Endpoint 2: Get Expired Contracts

`GET https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols`

Returns the expired Futures and Options contract symbols for one underlying and one expiry date. Futures and Options are returned separately.

### 3.1 Request parameters

| Parameter | Type | Required | Accepted values and notes |
|---|---|---|---|
| `symbol` | string | Yes | Underlying symbol. Same forms as endpoint 1: `NSE:NIFTY50-INDEX`, `NSE:SBIN-EQ`, `BSE:SENSEX-INDEX`. |
| `expiry_date` | string | Yes | `yyyy-mm-dd`. Documented instruction: use an expiry date returned by Get Expiry Dates. |

There is no `date_format` parameter here. The expiry date is always `yyyy-mm-dd`.

### 3.2 Response envelope

| Field | Type | Meaning |
|---|---|---|
| `s` | string | `ok` or `error` |
| `code` | int | Response code |
| `message` | string | Message |
| `data.symbol` | string | Underlying symbol for which contracts are returned |
| `data.expiry_date` | string | Expiry date of the returned contracts |
| `data.contracts.futures` | array of string | Expired Futures contract symbols for that expiry |
| `data.contracts.options` | array of string | Expired Options contract symbols for that expiry |

Verbatim documented success body:

```json
{
    "code": 200,
    "data": {
        "symbol": "SBIN",
        "expiry_date": "2025-03-27",
        "contracts": {
            "futures": ["NSE:SBIN25MARFUT"],
            "options": ["NSE:SBIN25MAR320PE", "NSE:SBIN25MAR320CE", "NSE:SBIN25MAR340PE", "NSE:SBIN25MAR340CE"]
        }
    },
    "message": "",
    "s": "ok"
}
```

Contract symbols come back fully qualified with the exchange prefix (`NSE:SBIN25MARFUT`), unlike `data.symbol`. Feed these strings straight into endpoint 3 without reconstructing them.

The options array in the sample is not sorted by strike and interleaves PE and CE. Do not assume any ordering. Sort in the pipeline if ordering matters.

There is no documented pagination, no count field, and no documented cap on how many option symbols one expiry can return. For a NIFTY weekly expiry this array can be very large. Stream and batch-insert rather than holding assumptions about size.

### 3.3 Verbatim Python sample

```python
from fyers_apiv3 import fyersModel

client_id = "XC4XXXXM-100"
access_token = "eyJ0eXXXXXXXX2c5-Y3RgS8wR14g"

fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token, log_path="")

data = {
    "symbol": "NSE:SBIN-EQ",
    "expiry_date": "2025-03-27"
}

response = fyers.history_underlying_symbols(data)
print(response)
print(response["data"]["contracts"]["futures"])
print(response["data"]["contracts"]["options"])
```

Verbatim cURL:

```bash
curl --location --request GET 'https://api-t1.fyers.in/data/history/fno/expired/underlying-symbols?symbol=NSE:SBIN-EQ&expiry_date=2025-03-27' \
 --header 'Authorization: app_id:access_token'
```

SDK method names: Python `fyers.history_underlying_symbols(data)`, Node and Web `fyers.get_history_underlying_symbols(reqBody)`, C# `GetUnderlyingSymbols(symbol, expiryDate)`, Java `GetHistoryUnderlyingSymbols(symbol, expiryDate)`, Go `GetHistoryUnderlyingSymbols(HistoryUnderlyingSymbolsRequest{UnderlyingSymbol, ExpiryDate})`, C `fyers_model_get_history_underlying_symbols`.

Note the C# SDK model exposes `Item1.Futures` and `Item1.Options` directly rather than nested under `contracts`. That is an SDK convenience, not the wire shape.

---

## 4. Endpoint 3: Get Expired F&O Data (historical candles)

`GET https://api-t1.fyers.in/data/history/fno/expired/historical-data`

### 4.1 Request parameters

| Parameter | Type | Required | Accepted values and notes |
|---|---|---|---|
| `symbol` | string | Yes | Expired Futures or Options contract symbol. Documented example: `NSE:NIFTY25MAR23000CE`. Use a value from endpoint 2. |
| `resolution` | string | Yes | Candle resolution. Documented set: `5S`, `1`, `2`, `3`, `5`, `10`, `15`, `20`, `30`, `45`, `60`, `120`, `180`, `240`. See 4.2. |
| `date_format` | int | Yes | Boolean flag. `0` for epoch range values, `1` for `yyyy-mm-dd` range values. Passed as string `"1"` in every SDK sample. |
| `range_from` | string | Yes | Start of range, epoch or `yyyy-mm-dd` per `date_format`. |
| `range_to` | string | Yes | End of range, epoch or `yyyy-mm-dd` per `date_format`. |
| `include_oi` | int | No | Set to `1` to include open interest in the response. Passed as string `"1"` in samples. When set, an `open_interest` column is appended and each candle gains a seventh element. |
| `include_greeks` | int | No | Documented as "(Coming soon)". Set to `1` to include greeks. Not usable today. Design the schema so greek columns can be added later without a rewrite. |

### 4.2 Resolution values, verbatim from the docs

| Meaning | Value |
|---|---|
| 5 seconds | `5S` |
| 1 minute | `1` |
| 2 minute | `2` |
| 3 minute | `3` |
| 5 minute | `5` |
| 10 minute | `10` |
| 15 minute | `15` |
| 20 minute | `20` |
| 30 minute | `30` |
| 45 minute | `45` |
| 60 minute | `60` |
| 120 minute | `120` |
| 180 minute | `180` |
| 240 minute | `240` |

`5S` is the only second-based value actually enumerated in the table, although the limits paragraph speaks generically about "second-based resolutions".

### 4.3 Documented limits

Quoted: "Currently, only the intraday timeframe is available; daily, weekly, and monthly timeframes will be coming soon."

- For `1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120, 180, 240` minute resolutions: "you can request up to 100 days of data at a time".
- For second-based resolutions (`5S`): "historical data is available only for the last 30 trading days".

The 100 day figure is not qualified as calendar or trading days. Treat it as calendar days, which is the conservative reading, and chunk at 100 calendar days or fewer.

The `5S` limit is not a per-request span limit, it is a data-availability window limit relative to today. That has a serious consequence for this project: 5-second data for contracts that expired more than 30 trading days ago is not retrievable at all. Any 5S backfill is a rolling capture that must run on a schedule, not a one-time historical download.

### 4.4 Data availability start dates

Documented twice, once for expiry data and once for historical data, with identical values.

| Exchange | Data available from |
|---|---|
| NSE | 03 Jan 2022 |
| BSE | 07 Aug 2023 |
| MCX | 03 Jan 2022 |

Quoted: "Historical data before the availability date for an exchange cannot be retrieved. The requested date range must also be within the limit for the selected resolution."

Clamp `range_from` to the exchange floor before dispatching, so the pipeline never burns a rate-limited request on a range that cannot return data.

### 4.5 Response envelope

This endpoint has a different envelope from the other two. There is no `data` wrapper, no `code`, and no `message` on success. Everything is top level.

| Field | Type | Meaning |
|---|---|---|
| `s` | string | `ok`, `no_data`, or `error`. This is the only one of the three endpoints that documents a `no_data` state. |
| `symbol` | string | Expired contract symbol for which data is returned |
| `resolution` | string | Resolution of the returned candles |
| `columns` | array of string | Column names in candle order: `timestamp, open, high, low, close, volume`, plus `open_interest` appended when `include_oi=1` |
| `candles` | array of array | One inner array per candle |
| `schema_version` | int | Version of the response schema. Documented value is `1`. |

Candle element order, documented: 1 current epoch time, 2 open value, 3 highest value, 4 lowest value, 5 close value, 6 total traded quantity (volume). The seventh element, when present, is open interest.

Verbatim documented success body with `include_oi=1`:

```json
{
    "candles": [
        [1742960700, 730.00, 750.00, 645.00, 706.00, 359100, 5131225],
        [1742964300, 706.00, 760.00, 651.30, 680.50, 246225, 5019500],
        [1742967900, 680.50, 708.85, 608.00, 640.00, 146025, 4987175],
        [1742971500, 636.90, 670.35, 594.50, 606.50, 376875, 4776350]
    ],
    "columns": ["timestamp", "open", "high", "low", "close", "volume", "open_interest"],
    "resolution": "60",
    "s": "ok",
    "schema_version": 1,
    "symbol": "NSE:NIFTY25MAR23000CE"
}
```

The C#, Java, and Go samples in the same document show the same call without `include_oi` and their responses have six columns and six-element candles:

```json
"columns": ["timestamp", "open", "high", "low", "close", "volume"]
```

Implementation rule: never index candles positionally against a hard-coded assumption. Read `columns` and map by name. That is the only safe way to survive `include_oi` being absent and `include_greeks` arriving later.

Timestamp semantics, derived from the sample and worth recording: `1742960700` is 2025-03-26 03:45:00 UTC, which is 09:15 IST, the NSE open. The next values are exactly 3600 seconds apart. So timestamps are epoch seconds, they mark the candle start (left edge), and 60 minute buckets are anchored to the 09:15 IST session open rather than to the clock hour. Store as epoch seconds and convert to Asia/Kolkata for display.

Documented note: "The API returns an error if historical data is not available for the specified contract, exchange, or date range." So a range with no data can surface either as an error envelope or as `s: "no_data"`. Handle both, and treat both as non-retryable for that particular chunk.

### 4.6 Verbatim Python sample

```python
from fyers_apiv3 import fyersModel

client_id = "XC4XXXXM-100"
access_token = "eyJ0eXXXXXXXX2c5-Y3RgS8wR14g"

fyers = fyersModel.FyersModel(client_id=client_id, is_async=False, token=access_token, log_path="")

data = {
    "symbol": "NSE:NIFTY25MAR23000CE",
    "resolution": "60",
    "date_format": "1",
    "range_from": "2025-03-26",
    "range_to": "2025-03-27",
    "include_oi": "1"
}

response = fyers.fno_historical_data(data)
print(response)
for candle in response["candles"]:
    print(candle)
```

Verbatim cURL:

```bash
curl --location --request GET 'https://api-t1.fyers.in/data/history/fno/expired/historical-data?symbol=NSE:NIFTY25MAR23000CE&resolution=60&date_format=1&range_from=2025-03-26&range_to=2025-03-27&include_oi=1' \
 --header 'Authorization: app_id:access_token'
```

SDK method names: Python `fyers.fno_historical_data(data)`, Node and Web `fyers.get_fno_historical_data(reqBody)`, C# `GetFnoHistoricalData(StockHistoryModel)`, Java `GetHistoryFNOExpired(HistoryFNOExpiredModel)`, Go `GetFNOHistoricalData(FNOHistoricalDataRequest{Symbol, Resolution, DateFormat, RangeFrom, RangeTo})`, C `fyers_model_get_history_fno_expired`.

Note that the Go and C# request models do not expose `include_oi` at all in the samples. If ExpiryManager ever uses an SDK rather than raw HTTP, verify OI support first. Raw `httpx` against the documented query string avoids the question entirely and is the recommended path here.

---

## 5. Symbol encoding rule that will bite

From the error code table in section 4 of the docs, on error `-300`: "Symbols containing special characters (ex: "M&M") must be URL-encoded (ex:"M%26M") when using direct URLs. This is handled automatically in the SDKs."

Since ExpiryManager should call the raw HTTP endpoints, every `symbol` value must be percent-encoded before being placed in the query string. `M&M` underlyings and any symbol containing `&` will silently break otherwise. Note that the documented cURL samples show unencoded `:` in `NSE:SBIN-EQ`, so the colon is accepted raw, but do not rely on that: encoding the colon as `%3A` is also valid per RFC 3986 in a query value. Use a proper query encoder and let it handle both.

---

## 6. Authentication and token refresh

### 6.1 Authorization header for every data call

Documented format: send the combination of app id and access token in the HTTP `Authorization` header, joined by a colon.

```
Authorization: app_id:access_token
```

Example from the docs: `Authorization: aaa-99:bbb`.

There is no `Bearer` prefix. This is not standard OAuth bearer usage. Send the literal `"{client_id}:{access_token}"` string.

### 6.2 Step 1, generate auth code

Navigate the user's browser to:

```
GET https://api-t1.fyers.in/api/v3/generate-authcode
```

| Parameter | Type | Notes |
|---|---|---|
| `client_id` | string | The app id received when creating the app, for example `SPXXXXE7-100`. |
| `redirect_uri` | string | Where the user is sent after login. Must exactly match the redirect URI registered at app creation time. |
| `response_type` | string | Must always be `code`. |
| `state` | string | A random value that is echoed back to the redirect URI. |

Verbatim documented cURL:

```bash
curl --location --request GET 'https://api-t1.fyers.in/api/v3/generate-authcode?client_id=SPXXXXE7-100&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html&response_type=code&state=sample_state'
```

The sample success value shown by the SDK is the URL itself, and it includes an extra `nonce` parameter that the SDK appends:

```
https://api-t1.fyers.in/api/v3/generate-authcode?
client_id=SPXXXXE7-100&
redirect_uri=https%3A%2F%2Fdev.fyers.in%2Fredirection%2Findex.html
&response_type=code&state=sample_state&nonce=sample_nonce
```

`nonce` is not in the documented request attribute table. It is optional in practice.

Redirect response attributes, delivered as query parameters on the redirect URI:

| Field | Type | Meaning |
|---|---|---|
| `s` | string | `ok` or `error` |
| `code` | int | Response code |
| `message` | string | Message for error responses |
| `auth_code` | string | Used to generate the access token |
| `state` | string | Returned as sent |

Documented best practice, quoted: "You should send a random value in the state parameter and verify whether the same value has been returned to you." This is the CSRF defence for the login leg and is mandatory for this project's security posture. Generate the state server side, store it against the pending session, verify on callback, and expire it.

Also documented: "Provide a redirect_uri which is in your control rather than a public endpoint such as google.com". For ExpiryManager the redirect URI should point at a FastAPI callback route on the local backend, and that same URI must be registered in the Fyers app.

### 6.3 Step 2, exchange auth code for access token

```
POST https://api-t1.fyers.in/api/v3/validate-authcode
Content-Type: application/json
```

Body:

| Field | Type | Notes |
|---|---|---|
| `grant_type` | string | Must always be `authorization_code` |
| `appIdHash` | string | SHA-256 hash, see 6.5 |
| `code` | string | The `auth_code` from step 1 |

Verbatim documented cURL:

```bash
curl --location --request POST 'https://api-t1.fyers.in/api/v3/validate-authcode' \
--header 'Content-Type: application/json' \
--data-raw '{
    "grant_type":"authorization_code",  
    "appIdHash":"c3efb1075ef2332b3a4ec7d44b0f05c1********************",
    "code":"eyJ0eXAi*******.eyJpc3MiOiJhcGkubG9********.r_65Awa1kGdsNTAgD******"
}'
```

Documented success response:

```json
{
  "s": "ok",
  "code": 200,
  "message": "",
  "access_token": "eyJ0eXAiOi***.eyJpc3MiOiJh***.HrSubihiFKXOpUOj_7***",
  "refresh_token": "eyJ0eXAiO***.eyJpc3MiOiJh***.67mXADDLrrleuEH_EE***"
}
```

The documented response attribute table for step 2 lists only `s`, `code`, `message`, and `access_token`. `refresh_token` appears in every sample body but is missing from the table. Treat `refresh_token` as present but optional in the parser.

Both tokens are JWTs (three dot-separated base64url segments). The access token payload therefore carries an `exp` claim that can be decoded locally without verification to schedule proactive refresh. That is an inference from the token shape, not a documented field.

### 6.4 Verbatim Python sample for step 2

```python
# Import the required module from the fyers_apiv3 package
from fyers_apiv3 import fyersModel

# Define your Fyers API credentials
client_id = "SPXXXXE7-100"  # Replace with your client ID
secret_key = "N********B"  # Replace with your secret key
redirect_uri = "https://trade.fyers.in/api-login/redirect-uri/index.html"  # Replace with your redirect URI
response_type = "code" 
grant_type = "authorization_code"  

# The authorization code received from Fyers after the user grants access
auth_code = "eyJ0eXAi*******.eyJpc3MiOiJhcGkubG9********.r_65Awa1kGdsNTAgD******"

# Create a session object to handle the Fyers API authentication and token generation
session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key, 
    redirect_uri=redirect_uri, 
    response_type=response_type, 
    grant_type=grant_type
)

# Set the authorization code in the session object
session.set_token(auth_code)

# Generate the access token using the authorization code
response = session.generate_token()

# Print the response, which should contain the access token and other details
print(response)
```

And step 1:

```python
# Import the required module from the fyers_apiv3 package
from fyers_apiv3 import fyersModel

# Replace these values with your actual API credentials
client_id = "SPXXXXE7-100"
secret_key = "N********B"
redirect_uri = "https://trade.fyers.in/api-login/redirect-uri/index.html"
response_type = "code"  
state = "sample_state"

# Create a session model with the provided credentials
session = fyersModel.SessionModel(
    client_id=client_id,
    secret_key=secret_key,
    redirect_uri=redirect_uri,
    response_type=response_type
)

# Generate the auth code using the session model
response = session.generate_authcode()

# Print the auth code received in the response
print(response)
```

### 6.5 How appIdHash is computed

The docs describe it twice, in slightly inconsistent prose, and the worked example settles it.

Quoted, step 2 table: "SHA-256 of api_id + app_secret. Eg: SHA-256 of app_id:app_secret is 7c7120d2b5004f8de22d8dc2da0453b4d7e6211e37a4108b8371266ecff00498".

Quoted, refresh token table: "SHA-256 of api_id + app_secret. Eg: SHA-256 of app_id:app_secret is c7120d2b5004f8de22d8dc2da0453b4d7e6211e37a4108b8371266ecff00498".

The phrase "api_id + app_secret" is loose wording. The worked example says "SHA-256 of app_id:app_secret", so the pre-image is the two values joined by a literal colon, and the digest is the lowercase hex encoding of the SHA-256.

```python
import hashlib

def app_id_hash(client_id: str, app_secret: str) -> str:
    # Fyers pre-image is the colon-joined pair, hex digest lowercase.
    return hashlib.sha256(f"{client_id}:{app_secret}".encode("utf-8")).hexdigest()
```

The two example digests in the docs differ by a leading `7`, so one of them is a typo. Both are 64 and 63 characters respectively, and a SHA-256 hex digest is always 64 characters, which confirms the second one lost a character in transcription. Do not treat either as a test vector.

`client_id` here is the full app id including the `-100` suffix, as shown in every sample (`SPXXXXE7-100`).

The C# SDK exposes this as `Utility.GenerateAppHashID(clientID, secretKey)`, which confirms the hash is derived from exactly those two inputs and nothing else, no redirect URI, no nonce.

### 6.6 Refresh token flow

```
POST https://api-t1.fyers.in/api/v3/validate-refresh-token
Content-Type: application/json
```

| Field | Type | Notes |
|---|---|---|
| `grant_type` | string | Must be `refresh_token` |
| `appIdHash` | string | Same SHA-256 as above |
| `refresh_token` | string | The refresh token issued by validate-authcode |
| `pin` | string | The user's Fyers PIN |

Verbatim documented cURL:

```bash
curl --location --request POST 'https://api-t1.fyers.in/api/v3/validate-refresh-token' \
--header 'Content-Type: application/json' \
--data-raw '{
  "grant_type": "refresh_token",
  "appIdHash": "c3efb1075ef2332b3a4ec7d44b0f05c1********************",
  "refresh_token": "eyJ0eXAiOiJKV1***.eyJpc3MiOiJhcGkuZn***.5_Qpnd1nQXBw1T_wNJNFF***",
  "pin": "****"
}'
```

Documented success response:

```json
{
    "s": "ok",
    "code": 200,
    "message": "",
    "access_token": "eyJ0eXAiOiJK***.eyJpc3MiOiJhcGkuZnllcnM***.IzcuRxg4tnXiULCx3***"
}
```

Documented facts about the refresh flow, all load bearing for the scheduler:

1. The refresh token has a validity of 15 days.
2. A new access token can be generated with it as long as it is still valid.
3. The refresh response returns only a new `access_token`. It does not return a rotated refresh token. So after 15 days the user must do the full browser login again.
4. The flow requires the user's PIN. This is a second secret beyond app id, app secret, and redirect URI, and it must therefore also be captured through the UI and encrypted at rest if unattended refresh is wanted at all.
5. Explicit deprecation notice, quoted: "Refresh token will be discontinued from 1st April." The year is not stated in this file, but the same document carries a SEBI note saying "changes to its usage may take effect on April 1, 2026". The safe reading is that refresh tokens go away on 1 April 2026.

Design consequence: do not build the scheduler on the assumption that tokens can be refreshed unattended forever. The pipeline must degrade to a "re-authenticate required" state, surface it clearly in the UI, and pause or park scheduled jobs rather than hammering the API with a dead token. Implement refresh as an optional optimisation behind a feature flag, with the interactive login as the guaranteed path.

Access token validity is not documented in these files. Fyers access tokens are day-scoped in practice, but that is not stated here. Derive expiry from the JWT `exp` claim rather than hard-coding a duration.

### 6.7 Documented best practices

1. Never share app_secret.
2. Never share access_token.
3. Do not grant trading permissions unless orders are being placed. ExpiryManager only needs the Market Data permission template (historical data, market depth, quotes), so the Fyers app should be created with the narrowest template that includes Market Data.
4. Use a redirect URI under your control.
5. Send a random `state` and verify it comes back.

---

## 7. Rate limits, blocking, and what that means for the scheduler

Documented table, verbatim values:

| Timeframe | Standard | Prime |
|---|---|---|
| Per second | 10 | 10 |
| Per minute | 200 | 600 |
| Per day | 1,00,000 (that is 100,000) | 2,00,000 (that is 200,000) |

Note that the per-second limit is 10 on both plans. Prime raises only the per-minute and per-day ceilings.

Critical, and easy to miss, quoted verbatim: "The user will be blocked for the rest of the day if the per minute rate limit is exceeded more than 3 times in the day."

This changes the design of the rate limiter. It is not enough to back off after a 429. Three per-minute violations in a day and the account is dead until midnight, which would destroy an overnight backfill. Requirements that follow:

1. A hard client-side token bucket at 10 requests per second AND a second bucket at 200 (or 600) per minute, applied before dispatch, shared across every worker and every scheduled job in the process. Never rely on the server to tell you that you are over.
2. Target a safety margin, for example 8 per second and 170 per minute on Standard, so clock skew and in-flight retries cannot push you over.
3. A persistent daily counter in SQLite, reset at IST midnight, that hard-stops the pipeline before 100,000.
4. A persistent violation counter. On the first observed `-429` or HTTP 429, stop the entire pipeline, mark the run degraded, and require either a long cooldown or manual resume. Two more violations means the day is lost.
5. The per-minute limiter must be process-wide and durable enough to survive a restart, otherwise a crash loop during a backfill can burn the three strikes in minutes.

Sizing note for planning: one NIFTY weekly expiry can have several hundred option contracts, and each contract at 1-minute resolution over a 100 day window is one request. At 8 requests per second the practical ceiling is roughly 28,800 requests per hour, and the per-minute cap of 200 is the real binding constraint at roughly 12,000 per hour on Standard. Budget backfills against 200 per minute, not against 10 per second.

There is also a regulatory note attached to the rate limit section, quoted: "Due to SEBI's retail algo trading regulations, changes to its usage may take effect on April 1, 2026." Rate limits are subject to change on that date.

---

## 8. Errors and retry policy

### 8.1 HTTP status codes, documented

| Status | Meaning |
|---|---|
| 200 | Request was successful |
| 400 | Bad request. The request is invalid or certain other errors |
| 401 | Authorization error. User could not be authenticated |
| 403 | Permission error. User does not have the necessary permissions |
| 429 | Rate limit exceeded. Users have been blocked for exceeding the rate limit. |
| 500 | Internal server error. |

### 8.2 Response envelope on failure

| Field | Type | Meaning |
|---|---|---|
| `s` | string | `error` |
| `code` | int | Negative integer identifying the specific error |
| `message` | string | Error message |

Success envelope for the general case: `s` is `ok`, `code` is `200`, `message` is empty, plus request-specific keys. Remember that the historical-data endpoint deviates and has no `code` or `message` on success.

### 8.3 Documented error codes and retry classification

| Code | Documented description | Class | Action |
|---|---|---|---|
| -8 | Token is expired | Auth, recoverable once | Refresh or re-authenticate, then retry the request once. Do not retry blindly. |
| -15 | Invalid token provided | Auth, fatal for the token | Mark credentials invalid, stop the pipeline, prompt re-login. No retry. |
| -16 | Server unable to authenticate user token | Auth, ambiguous | Treat like -8 first. If it repeats after a fresh token, treat as fatal. |
| -17 | Token passed is either invalid or expired | Auth, recoverable once | Same as -8. |
| -50 | One or more invalid parameters passed. The `message` field names the specific invalid inputs. | Client error, fatal | Never retry. Log the full request and the `message`, mark the chunk permanently failed, and surface it in the UI. This is the code you will hit for an out-of-range date span or a bad resolution. |
| -51 | Invalid Order ID | Not applicable to this project | Ignore. |
| -53 | Invalid position ID | Not applicable | Ignore. |
| -99 | Order placement rejected | Not applicable | Ignore. |
| -300 | Invalid symbol provided. Symbols with special characters such as `M&M` must be URL-encoded (`M%26M`) on direct URLs. | Client error, fatal, but often an encoding bug | Do not retry the same URL. Verify percent-encoding first. If encoding was correct, mark the symbol as unsupported in the catalog. |
| -352 | Invalid App ID provided | Config, fatal | Stop everything, the stored credentials are wrong. Prompt for re-entry. |
| -352 | Also documented as: no position available to exit (exit position API) | Not applicable | The code is overloaded. In this project only the App ID meaning applies. |
| -429 | API rate limit exceeded, per second, per minute, or per day | Rate limited | Stop the whole pipeline, not just the one request. Increment the persistent violation counter. See section 7. |
| 400 | Multi leg order placement invalid input | Not applicable | Ignore. |

Codes not in the table but relevant to a real client:

- HTTP 500 and any 5xx: retryable with exponential backoff and jitter, capped at a small number of attempts. Not documented as retryable, but it is a server error by definition.
- Network timeouts and connection resets: retryable with backoff.
- HTTP 403: permission error. This is what you get if the Fyers app was created without the Market Data permission template. Fatal, and the UI should say exactly that.
- `s: "no_data"` on historical-data: not an error. Record the chunk as fetched-and-empty so the scheduler does not re-request it forever. Distinguish "no data because the contract did not trade that day" from "not yet fetched".

### 8.4 Recommended retry policy summary

- Fatal, no retry, mark permanently failed: -50, -300, -352, HTTP 400, HTTP 403.
- Auth recovery, refresh once then retry once: -8, -16, -17.
- Auth fatal, stop and prompt re-login: -15, HTTP 401, and -16 after a failed refresh.
- Rate limited, stop the pipeline and cool down: -429, HTTP 429.
- Transient, exponential backoff with jitter, maximum 3 to 5 attempts: HTTP 5xx, timeouts, connection errors.
- Empty result, record and move on: `s: "no_data"`.

Every retry consumes rate-limit budget. Count retries against the same buckets as fresh requests.

---

## 9. Metadata worth persisting

This project wants maximum metadata capture. The three responses are thin, so most of the metadata value comes from three places: what the API returned, what was requested, and what can be derived from the symbol string.

### 9.1 Directly from responses

Expiry dates response:

- Requested symbol as sent (canonical key).
- `data.symbol` echo (note it is stripped, keep it for provenance only).
- `data.from_date`, `data.to_date` echo.
- Each entry of `data.expiry_dates.futures` and `data.expiry_dates.options`, with a flag for which list it came from. An expiry can appear in both lists, as it does in the sample. Model this as a set of (underlying, expiry_date, has_futures, has_options) rather than two disjoint tables.
- Envelope `code`, `message`, `s`.

Expired contracts response:

- `data.expiry_date`.
- Every string in `data.contracts.futures` and `data.contracts.options`, with instrument class from which array it came.
- Position within the returned array, if you want to reproduce the API's ordering exactly.
- The count of futures and options contracts for that expiry. This is a genuinely useful research statistic (strike chain width per expiry over time).

Historical data response:

- `symbol`, `resolution`, `schema_version`, `s`.
- `columns` verbatim. Store the actual column list per fetch, because it changes with `include_oi` and will change again with greeks. Do not normalise it away.
- Candles: epoch timestamp, open, high, low, close, volume, and open_interest when present.

### 9.2 From the request and the fetch itself (provenance)

- Full request URL or the parameter set, `resolution`, `date_format`, `range_from`, `range_to`, `include_oi`, `include_greeks`.
- `fetched_at` in UTC, plus the IST trading date it covers.
- HTTP status, response latency, response byte size.
- Candle count returned, first candle timestamp, last candle timestamp.
- Which credential or app id the fetch used.
- Job id, run id, attempt number, and whether it was a backfill or a scheduled incremental.
- A content hash of the candle payload, so re-fetches can be detected as identical rather than rewritten.

Together these give a coverage ledger: for every (contract, resolution, date range) you know whether it was never fetched, fetched with data, fetched empty, or failed and why. That ledger is what makes a resumable, idempotent scheduler possible, and it belongs in SQLite next to the job state, while the candles go to DuckDB.

### 9.3 Derived from the symbol string (appendix 29 symbology)

The contract symbol encodes a lot, and parsing it is the cheapest metadata the project will ever get. Formats:

- Equity futures: `{Ex}:{UnderlyingSymbol}{YY}{MMM}FUT`, for example `NSE:NIFTY20OCTFUT`, `BSE:SENSEX23AUGFUT`.
- Options, monthly expiry: `{Ex}:{UnderlyingSymbol}{YY}{MMM}{Strike}{Opt_Type}`, for example `NSE:NIFTY20OCT11000CE`.
- Options, weekly expiry: `{Ex}:{UnderlyingSymbol}{YY}{M}{dd}{Strike}{Opt_Type}`, for example `NSE:NIFTY2010811000CE` and `NSE:NIFTY20O0811000CE`.
- Equity: `{Ex}:{Symbol}-{Series}`, for example `NSE:SBIN-EQ`.
- Index: `NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`, `BSE:SENSEX-INDEX`.

Weekly month character mapping, which is the part that trips up naive parsers: Jan to Sep are `1` to `9`, Oct is the letter `O`, Nov is `N`, Dec is `D`. So `NSE:NIFTY20O0811000CE` is 8 October 2020 and `NSE:NIFTY2010811000CE` is 8 January 2020. The letter `O` versus the digit `0` distinction is real and must be handled explicitly.

Fields to derive and persist per contract: exchange, underlying root, expiry date (validated against the expiry date the contract was discovered under, which is the reliable source), monthly or weekly flag, instrument class (FUT, CE, PE), strike price (may be fractional, as in `80.5`), and option type.

Prefer the expiry date from the Get Expired Contracts request over the parsed one. The parsed one is a cross-check that catches symbology surprises.

### 9.4 Reference tables worth seeding into SQLite

From appendix 29, useful as lookup tables for a maximum-metadata catalog:

- Exchange codes: NSE 10, MCX 11, BSE 12.
- Segment codes: Capital Market 10, Equity Derivatives 11, Currency Derivatives 12, Commodity Derivatives 20.
- Instrument types relevant here: 10 INDEX, 0 EQ, 11 FUTIDX, 13 FUTSTK, 14 OPTIDX, 15 OPTSTK.
- Fytoken layout: 2 digit exchange, 2 digit segment, 6 digit expiry as YYMMDD, then 2 to 6 digit exchange token. The expired endpoints do not return fytokens, but the symbol master from the Data API does, and joining on it later is the way to enrich the catalog with lot size, tick size, and exchange tokens.

### 9.5 What these endpoints do NOT give you

Worth stating plainly so nobody plans around it:

- No lot size, no tick size, no freeze quantity.
- No fytoken or exchange token.
- No settlement price, no expiry-day settlement flag.
- No greeks and no implied volatility yet (`include_greeks` is "coming soon").
- No underlying spot price alongside the option candle.
- No bid, ask, or depth. Only OHLCV plus optional OI.
- No daily, weekly, or monthly candles yet. Any daily series must be aggregated from intraday inside DuckDB.

The lot size and tick size gap matters for a Phase 2 backtesting engine. Plan to source them from the Fyers symbol master (docs section 23) and store them in the SQLite catalog keyed by contract, versioned by date, because lot sizes change over time.

---

## 10. Implementation checklist derived from the above

1. Raw HTTP with `httpx`, not the SDK. The SDK hides the wire shape, its request models omit `include_oi` in several languages, and this project needs exact control over the rate limiter.
2. One shared async rate limiter: 10 per second and 200 (or 600) per minute, both durable, plus a daily counter and a violation counter in SQLite.
3. Chunk historical-data requests to 100 calendar days maximum for minute resolutions. Never request `5S` for anything older than 30 trading days.
4. Clamp every `range_from` to the exchange availability floor: NSE and MCX 03 Jan 2022, BSE 07 Aug 2023.
5. Chunk expiry-dates requests to 366 days maximum. For a full NSE history from 03 Jan 2022 to today that is four or five calls per underlying.
6. Always pass `date_format=1` and `yyyy-mm-dd`, matching every documented sample, and always pass `include_oi=1`. OI is free metadata and the project wants maximum capture.
7. Map candle columns by reading the `columns` array, never by fixed index.
8. Percent-encode the `symbol` query parameter.
9. Store epoch seconds as returned, convert to Asia/Kolkata only at the presentation layer.
10. Store the app id, app secret, redirect URI, and (if unattended refresh is wanted) the PIN encrypted at rest in SQLite. Never log the access token, refresh token, app secret, or PIN, and redact them from any request logging.
11. Verify the `state` parameter on the OAuth callback. Generate it server side, single use, short expiry.
12. Treat the refresh token flow as best effort and deprecated from 1 April 2026. The guaranteed path is interactive re-login, and the scheduler must park jobs and raise a visible "re-authentication required" state rather than failing silently.
13. Build the coverage ledger from day one. It is what makes backfills resumable and idempotent, and it is cheap to add now and expensive to retrofit.

---

## 11. Open questions the design must decide or probe

1. Is the 100 day historical-data limit calendar days or trading days? Not documented. Chunk at 100 calendar days to stay safe, and confirm empirically.
2. Is the 366 day expiry-dates window enforced server side with an error, or silently truncated? Not documented. Chunk defensively and cross-check the echoed `from_date` and `to_date`.
3. What exactly does the API return when the range partly predates the exchange availability floor? Documented as "cannot be retrieved", but whether that is an error or a truncated result is unstated.
4. Are second-based resolutions other than `5S` accepted? Only `5S` is enumerated, while the limits paragraph says "second-based resolutions" plural.
5. What is the actual access token lifetime? Not documented. Plan to read the JWT `exp` claim.
6. Does the expired API cover MCX? The availability table lists MCX, but every sample is NSE, and the endpoint is named "fno". Needs a live probe before promising commodity coverage.
7. Does `data.symbol` in the expiry-dates and underlying-symbols responses ever return the fully qualified form for index underlyings such as `NSE:NIFTY50-INDEX`? The only sample is an equity and it came back as bare `SBIN`.
8. Is there any cap or pagination on the options contract array for a large index weekly expiry? None documented.
9. Does the server send rate-limit headers (remaining, reset)? Not documented. If it does, they are worth capturing into the limiter.
10. Exact year for the refresh token discontinuation. The file says "1st April" without a year, and the neighbouring SEBI note says 1 April 2026.
11. Which Fyers permission template is actually required for the expired endpoints? Market Data is the obvious candidate, but the permission table does not name the expired endpoints explicitly.
12. Does `include_greeks=1` currently error with -50 or silently no-op? It is marked coming soon. Guard it behind a capability flag.
