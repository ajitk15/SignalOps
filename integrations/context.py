from __future__ import annotations
import json, os, ssl, urllib.parse, urllib.request

def _get(url: str, token: str, prefix: str = "Bearer", ndjson: bool = False):
    req = urllib.request.Request(url, headers={"Authorization": f"{prefix} {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as response:
        body = response.read().decode()
        if ndjson:
            return [json.loads(line) for line in body.splitlines() if line.strip()]
        return json.loads(body)

class SplunkReader:
    def __init__(self, base_url: str, token: str): self.base_url, self.token = base_url.rstrip("/"), token
    def search(self, query: str, earliest: str = "-30m"):
        args = urllib.parse.urlencode({"search": f"search {query}", "earliest_time": earliest, "output_mode": "json"})
        return _get(f"{self.base_url}/services/search/jobs/export?{args}", self.token, "Splunk", ndjson=True)

class DynatraceReader:
    def __init__(self, base_url: str, token: str): self.base_url, self.token = base_url.rstrip("/"), token
    def problems(self, entity_selector: str, minutes: int = 30):
        args = urllib.parse.urlencode({"from": f"now-{minutes}m", "entitySelector": entity_selector})
        return _get(f"{self.base_url}/api/v2/problems?{args}", self.token, "Api-Token")

def readers_from_env():
    splunk = SplunkReader(os.environ["SPLUNK_BASE_URL"], os.environ["SPLUNK_TOKEN"]) if os.getenv("SPLUNK_BASE_URL") and os.getenv("SPLUNK_TOKEN") else None
    dynatrace = DynatraceReader(os.environ["DYNATRACE_BASE_URL"], os.environ["DYNATRACE_TOKEN"]) if os.getenv("DYNATRACE_BASE_URL") and os.getenv("DYNATRACE_TOKEN") else None
    return splunk, dynatrace
