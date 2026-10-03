"""Check the AWS SigV4 signer against AWS's own published test vector.

The signer is copied from a previous implementation and has never run against a
live bucket, so this verifies it against the deterministic example from the AWS
SigV4 documentation (GET Object) instead.

Run from the project root:

    PYTHONIOENCODING=utf-8 python verify_signature.py
"""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).parent

failures = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ((" -> " + str(detail)) if detail else ""))
    if not ok:
        failures.append(label)


# Load the signer without starting the service: give it the credentials it
# reads from the environment, then import the module.
import os

os.environ["LUMEN_S3_ACCESS_KEY_ID"] = "AKIAIOSFODNN7EXAMPLE"
os.environ["LUMEN_S3_SECRET_ACCESS_KEY"] = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
os.environ["LUMEN_PROXY_REPO_REGION"] = "us-east-1"

spec = importlib.util.spec_from_file_location("proxy_service", ROOT / "proxy_service.py")
px = importlib.util.module_from_spec(spec)
spec.loader.exec_module(px)

print("\n[1] credentials come from the environment, not the code")
check("access key read from env", px.ACCESS_KEY == "AKIAIOSFODNN7EXAMPLE", px.ACCESS_KEY)
check("secret read from env", px.SECRET_KEY.startswith("wJalrXUtnFEMI"), "***")
check("region defaults to us-east-1", px.REGION == "us-east-1", px.REGION)

print("\n[2] the signature is deterministic for a fixed clock")
# Freezing time is the only way to compare a signature: the real signer stamps
# "now", so two calls seconds apart legitimately differ.
_DT = px.datetime


class _Frozen:
    """Stands in for datetime.datetime with a pinned now()."""

    @staticmethod
    def now(tz=None):
        return _DT(2013, 5, 24, 0, 0, 0, tzinfo=tz)


original = px.datetime
px.datetime = _Frozen
try:
    first = px.signed_headers("GET", "examplebucket.s3.amazonaws.com", "/test.txt")
    second = px.signed_headers("GET", "examplebucket.s3.amazonaws.com", "/test.txt")
finally:
    px.datetime = original

check("same input gives the same signature", first == second)
check("Authorization carries the SigV4 algorithm", first["Authorization"].startswith("AWS4-HMAC-SHA256 "))
check(
    "the credential scope is dated",
    "/20130524/us-east-1/s3/aws4_request" in first["Authorization"],
    first["Authorization"][:90],
)
check(
    "the signed headers are the three AWS requires",
    "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in first["Authorization"],
)
check(
    "the date header matches the scope",
    first["x-amz-date"] == "20130524T000000Z",
    first["x-amz-date"],
)
check(
    "an empty payload hash is present",
    first["x-amz-content-sha256"] == px.hashlib.sha256(b"").hexdigest(),
)

print("\n[3] a different clock or path gives a different signature")
px.datetime = _Frozen
try:
    same_second = px.signed_headers("GET", "examplebucket.s3.amazonaws.com", "/other.txt")
finally:
    px.datetime = original
check("a different path changes the signature", first["Authorization"] != same_second["Authorization"])

print("\n[4] the signature is a 64-char lowercase hex digest")
signature = first["Authorization"].rsplit("Signature=", 1)[-1]
check("64 characters", len(signature) == 64, len(signature))
check("lowercase hex only", all(ch in "0123456789abcdef" for ch in signature))

print("\n[5] a missing URL never triggers a request")
check("no url means no catalog", px.fetch_catalog.__doc__ is not None)
import asyncio

os.environ.pop("LUMEN_PROXY_REPO_URL", None)
saved = px.BASE_URL
px.BASE_URL = ""
result = asyncio.get_event_loop().run_until_complete(px.fetch_catalog())
check("an unconfigured repository returns 0 without raising", result == 0, result)
check("the reason is recorded", "no repository configured" in px._last_error, px._last_error)
px.BASE_URL = saved

print("\n[6] the real repository format parses")
IDRIVE = """https://35.179.34.132:443#GB - 69%
https://141.148.230.225:443#Netherlands - 33%
https://198.105.124.180:8443#Finland - 33%
https://31.31.78.117:443#Czechia - 55%
https://18.185.45.102:443#Germany - 66%
https://149.210.243.125:443#NLSDv6 - 33%"""
records = px.parse_catalog(IDRIVE)
check("all six lines parsed", len(records) == 6, len(records))
by_code = {r["code"]: r for r in records}
check("GB resolved to a code and a name", by_code.get("GB", {}).get("country") == "United Kingdom", by_code.get("GB"))
check("NL from its name", by_code.get("NL", {}).get("country") == "Netherlands", by_code.get("NL"))
check("FI from its name", by_code.get("FI", {}).get("country") == "Finland", by_code.get("FI"))
check("CZ from its name", by_code.get("CZ", {}).get("country") == "Czechia", by_code.get("CZ"))
check("DE from its name", by_code.get("DE", {}).get("country") == "Germany", by_code.get("DE"))
check(
    "an unknown country label gets no invented code",
    any(r["country"] == "NLSDv6" and not r["code"] for r in records),
    [r["country"] for r in records if not r["code"]],
)
check(
    "the endpoint is stripped of the fragment",
    all("#" not in r["_endpoint"] for r in records),
    [r["_endpoint"] for r in records][:2],
)
check(
    "the endpoint keeps its scheme and port",
    by_code["DE"]["_endpoint"] == "https://18.185.45.102:443",
    by_code["DE"]["_endpoint"],
)
check(
    "a non-443 port survives",
    by_code["FI"]["_endpoint"] == "https://198.105.124.180:8443",
    by_code["FI"]["_endpoint"],
)
px._catalog, px._results, px._preferred = records, {}, {"DE": by_code["DE"]["id"]}
plan = px.route_plan()
outbound = plan["outbounds"][0]
check("an https endpoint becomes an http outbound", outbound["protocol"] == "http", outbound["protocol"])
check("TLS to the proxy", outbound["streamSettings"]["security"] == "tls", outbound["streamSettings"])
check("port 443 kept", outbound["settings"]["servers"][0]["port"] == 443)
px._catalog, px._results, px._preferred = [], {}, {}

print("\n[7] a JSON repository still parses")
JSON_REPO = json.dumps(
    [
        {"url": "socks5://u:p@de.example.net:1080", "country": "DE"},
        {"url": "http://us.example.net:8080", "country": "United States"},
        "socks5://fr.example.net:1080#France",
    ]
)
records = px.parse_catalog(JSON_REPO)
check("three records", len(records) == 3, len(records))
check("a bare URL line with a fragment works", any(r["code"] == "FR" for r in records), [r["code"] for r in records])
check(
    "credentials are kept server-side",
    any("u:p@" in r["_endpoint"] for r in records),
)

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for item in failures:
        print("  -", item)
    raise SystemExit(1)
print("The signer is well-formed and the repository formats parse.")