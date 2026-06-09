"""AdLabs MCP client — talks to the AdLabs MCP server over Streamable HTTP
(JSON-RPC 2.0) so the Flask app can drive AdLabs directly.

Transport: POST {ADLABS_MCP_URL}; auth via the X-ADLABS-MCP-KEY header. The
server is stateless (no Mcp-Session-Id). Responses come back either as
application/json or as an SSE stream (`data: {json}`) — both are handled.

Two session concepts:
  - MCP `initialize` handshake (once per client).
  - AdLabs `chat_session_id` from the start_chat_session tool (passed on every
    subsequent tool call).

Tool results are returned as text content blocks; helpers extract the AdLabs
data-reference URIs (mcp://data/...) and the chat_session_id from that text.
"""

import json
import re
import threading
import time

import requests

from config import cfg

_REF_RE = re.compile(r"mcp://data/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SID_RE = re.compile(r"chat_session_id[\"']?\s*[:=]?\s*[\"']?([0-9a-f-]{36})", re.I)


class AdLabsError(RuntimeError):
    pass


class AdLabsClient:
    def __init__(self, url=None, key=None):
        self.url = (url or cfg.ADLABS_MCP_URL)
        self.key = key or cfg.ADLABS_MCP_KEY
        self._id = 0
        self._initialized = False
        self._chat_session_id = None
        self._lock = threading.Lock()

    # -- transport --------------------------------------------------------

    def _headers(self):
        return {
            "X-ADLABS-MCP-KEY": self.key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def _next_id(self):
        self._id += 1
        return self._id

    def _post(self, method, params=None, is_notification=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if not is_notification:
            payload["id"] = self._next_id()
        if params is not None:
            payload["params"] = params
        resp = requests.post(self.url, headers=self._headers(),
                             data=json.dumps(payload), timeout=120)
        if is_notification:
            return None
        if resp.status_code >= 400:
            raise AdLabsError(f"{method} HTTP {resp.status_code}: {resp.text[:300]}")
        return self._parse_body(resp)

    @staticmethod
    def _parse_body(resp):
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            # Concatenate SSE `data:` lines and parse the last JSON object.
            data_lines = [ln[5:].strip() for ln in resp.text.splitlines()
                          if ln.startswith("data:")]
            for chunk in reversed(data_lines):
                try:
                    return json.loads(chunk)
                except ValueError:
                    continue
            raise AdLabsError(f"Could not parse SSE body: {resp.text[:300]}")
        return resp.json()

    def _ensure_init(self):
        with self._lock:
            if self._initialized:
                return
            self._post("initialize", {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ngram-suite", "version": "1.0"},
            })
            self._post("notifications/initialized", is_notification=True)
            self._initialized = True

    # -- core calls -------------------------------------------------------

    def call_tool(self, name, arguments=None, max_retries=5):
        """Call an MCP tool; return the concatenated text content. Raises on error.

        AdLabs throttles per-tool ("rate limit exceeded …"); back off and retry
        transparently instead of surfacing the error to the user.
        """
        self._ensure_init()
        for attempt in range(max_retries + 1):
            result = self._post("tools/call", {"name": name, "arguments": arguments or {}})
            if "error" in result:
                raise AdLabsError(f"{name}: {result['error']}")
            res = result.get("result") or {}
            content = res.get("content", [])
            text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
            if res.get("isError"):
                if "rate limit" in text.lower() and attempt < max_retries:
                    time.sleep(min(20, 3 * (attempt + 1)))
                    continue
                raise AdLabsError(f"{name}: {text}")
            return text

    def read_resource(self, uri):
        # AdLabs exposes resource reading as a TOOL (not the MCP resources/read RPC).
        return self.tool("read_resource", uri=uri)

    # -- AdLabs session ---------------------------------------------------

    @property
    def session_id(self):
        if not self._chat_session_id:
            text = self.call_tool("start_chat_session")
            m = _SID_RE.search(text)
            if not m:
                raise AdLabsError(f"Could not parse chat_session_id from: {text[:200]}")
            self._chat_session_id = m.group(1)
        return self._chat_session_id

    def tool(self, name, **arguments):
        """Call a tool with chat_session_id auto-injected."""
        arguments["chat_session_id"] = self.session_id
        return self.call_tool(name, arguments)

    # -- high-level helpers ----------------------------------------------

    def get_entity_data(self, entity_type, team_id=None, profile_id=None, filters=None):
        args = {"entity_type": entity_type}
        if team_id is not None:
            args["team_id"] = team_id
        if profile_id is not None:
            args["profile_id"] = profile_id
        if filters is not None:
            args["filters"] = filters if isinstance(filters, str) else json.dumps(filters)
        return self.tool("get_entity_data", **args)

    def read(self, reference, limit=100):
        return self.tool("read", reference=reference, limit=limit)

    def query(self, reference, sql):
        return self.tool("query", reference=reference, query=sql)

    def group_by(self, reference, columns):
        return self.tool("group_by_column", reference=reference, columns=columns)

    def download_csv(self, reference):
        return self.tool("download_data", reference=reference)

    def download_rows(self, reference):
        """Full result set as list[dict]. `download_data` returns a short-lived
        download URL (no 100-row cap like `read`); fetch + parse the CSV."""
        import csv
        import io
        text = self.download_csv(reference)
        m = re.search(r"https://\S+", text or "")
        if not m:
            return []
        resp = requests.get(m.group(0).rstrip(").,"), timeout=180)
        resp.raise_for_status()
        return list(csv.DictReader(io.StringIO(resp.text)))

    def update_entities(self, **arguments):
        return self.tool("update_entities", **arguments)

    def create_entities(self, **arguments):
        return self.tool("create_entities", **arguments)

    # -- parsing helpers --------------------------------------------------

    @staticmethod
    def references(text):
        """Return the data references found in a tool result (row-level first)."""
        return _REF_RE.findall(text or "")

    @staticmethod
    def first_reference(text):
        refs = AdLabsClient.references(text)
        return refs[0] if refs else None

    @staticmethod
    def parse_table(text):
        """Parse a `read` tool TSV result into a list of dicts. Non-table trailing
        lines (e.g. 'View in AdLabs: …') have no tabs and are dropped."""
        import csv
        import io
        lines = [ln for ln in (text or "").replace("\r", "").splitlines() if "\t" in ln]
        if not lines:
            return []
        return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t"))


def health_check():
    """Quick connectivity/auth probe used by the UI. Returns (ok, message)."""
    c = AdLabsClient()
    try:
        c._ensure_init()
    except AdLabsError as e:
        return False, f"Cannot reach AdLabs MCP: {e}"
    try:
        c.call_tool("start_chat_session")
        return True, "AdLabs MCP connected."
    except AdLabsError as e:
        return False, str(e)
