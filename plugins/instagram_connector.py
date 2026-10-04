import json
import hmac
import hashlib
import time
import threading
import requests
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


# ============================================================
# NEXUS PLUGIN METADATA
# ============================================================

PLUGIN = {
    "name": "instagram_connector",
    "version": "1.3",
    "description": (
        "Control the NEXUS Instagram connector. "
        "Check Instagram status, enable or disable the connector, "
        "enable or disable automatic busy replies, change the busy reply message, "
        "start or stop the webhook, or send a configured test message."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Action to perform. Use one of: "
                    "status, enable, disable, set_busy, clear_busy, "
                    "start_webhook, stop_webhook, send_test."
                )
            },
            "message": {
                "type": "STRING",
                "description": (
                    "Optional message. For set_busy, this becomes the automatic "
                    "Instagram reply message. For send_test, this is the test message."
                )
            }
        },
        "required": ["action"]
    }
}


# ============================================================
# GLOBALS
# ============================================================

_SERVER = None
_SERVER_THREAD = None
_LAST_REPLY = {}
_REPLY_LOCK = threading.RLock()


# ============================================================
# PATH / CONFIG
# ============================================================

def get_base_dir() -> Path:
    """Return NEXUS project root directory."""
    return Path(__file__).resolve().parent.parent


def _config_path() -> Path:
    """Return config/instagram.json path."""
    path = get_base_dir() / "config" / "instagram.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _default_config() -> dict:
    return {
        "enabled": True,
        "busy_mode": True,
        "busy_message": "Boss is currently busy",
        "auto_reply": True,
        "reply_cooldown_seconds": 300,

        "webhook_host": "0.0.0.0",
        "webhook_port": 8080,

        "verify_token": "",
        "access_token": "",
        "app_secret": "",
        "instagram_user_id": "",

        "graph_api_base": "https://graph.instagram.com",
        "graph_api_version": "v23.0",

        "test_recipient_id": "",

        # Set true to print full webhook payloads (contains DM text / user IDs).
        "debug_payload": False
    }


def _load_config() -> dict:
    """Load Instagram configuration."""

    cfg = _default_config()
    file_path = _config_path()

    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
        except Exception as e:
            print(f"[Instagram] Error reading config: {e}")

    # Load sensitive values from NEXUS config manager if available.
    try:
        from memory.config_manager import load_api_keys

        keys = load_api_keys() or {}

        if keys.get("instagram_access_token"):
            cfg["access_token"] = keys["instagram_access_token"]
        if keys.get("instagram_app_secret"):
            cfg["app_secret"] = keys["instagram_app_secret"]
        if keys.get("instagram_verify_token"):
            cfg["verify_token"] = keys["instagram_verify_token"]

    except Exception:
        pass

    return cfg


def _save_config(cfg: dict) -> bool:
    """Save Instagram configuration."""

    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as e:
        print(f"[Instagram] Error saving config: {e}")
        return False


def _safe_equals(a, b) -> bool:
    """Timing-safe string comparison (handles None and non-ASCII)."""
    return hmac.compare_digest(
        (a or "").encode("utf-8"),
        (b or "").encode("utf-8")
    )


# ============================================================
# SEND INSTAGRAM MESSAGE
# ============================================================

def send_instagram_message(recipient_id: str, text: str, cfg: dict) -> dict:
    """Send an Instagram DM through the Graph API."""

    access_token = cfg.get("access_token")
    api_base = cfg.get("graph_api_base", "https://graph.instagram.com").rstrip("/")
    api_version = cfg.get("graph_api_version", "v23.0").strip("/")

    if not access_token:
        return {"success": False, "error": "Missing access_token"}

    url = f"{api_base}/{api_version}/me/messages"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)

        try:
            res_json = res.json()
        except Exception:
            res_json = {"raw_response": res.text}

        if res.status_code in (200, 201):
            return {"success": True, "data": res_json}

        return {
            "success": False,
            "status_code": res.status_code,
            "error": res_json
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================
# WEBHOOK MESSAGE EXTRACTION
# ============================================================

def _extract_messages(data: dict) -> list:
    """
    Extract incoming Instagram DM events.

    Expected structure:
    {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "..."},
                        "recipient": {"id": "..."},
                        "message": {"text": "hello"}
                    }
                ]
            }
        ]
    }
    """

    messages = []

    if not isinstance(data, dict):
        return messages

    if data.get("object") != "instagram":
        print(
            f"[Instagram] Webhook object was "
            f"{data.get('object')!r}, not 'instagram'."
        )
        return messages

    entries = data.get("entry", [])

    if not isinstance(entries, list):
        print("[Instagram] Webhook 'entry' is not a list.")
        return messages

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        messaging_events = entry.get("messaging", [])
        if not isinstance(messaging_events, list):
            continue

        for messaging in messaging_events:
            if not isinstance(messaging, dict):
                continue

            sender = messaging.get("sender", {})
            recipient = messaging.get("recipient", {})
            message_data = messaging.get("message", {})

            if not isinstance(sender, dict):
                sender = {}
            if not isinstance(recipient, dict):
                recipient = {}
            if not isinstance(message_data, dict):
                message_data = {}

            sender_id = sender.get("id")
            recipient_id = recipient.get("id")
            text = message_data.get("text")
            is_echo = message_data.get("is_echo", False)

            if sender_id and text and not is_echo:
                messages.append({
                    "sender_id": sender_id,
                    "recipient_id": recipient_id,
                    "text": text,
                    "timestamp": messaging.get("timestamp")
                })

    return messages


# ============================================================
# AUTO REPLY
# ============================================================

def _handle_incoming_messages(messages: list, cfg: dict):
    """Process incoming Instagram messages."""

    if not cfg.get("enabled"):
        print("[Instagram] Connector disabled.")
        return

    if not cfg.get("auto_reply"):
        print("[Instagram] Auto-reply disabled.")
        return

    if not cfg.get("busy_mode"):
        print("[Instagram] Busy mode disabled.")
        return

    cooldown = cfg.get("reply_cooldown_seconds", 300)
    busy_msg = cfg.get("busy_message", "Boss is currently busy")

    for msg in messages:
        sender_id = msg["sender_id"]
        now = time.time()

        # Check cooldown under the lock, but never hold it during network I/O.
        with _REPLY_LOCK:
            if now - _LAST_REPLY.get(sender_id, 0) < cooldown:
                print(
                    f"[Instagram] Cooldown active for sender "
                    f"{sender_id}. Skipping auto-reply."
                )
                continue

        print(f"[Instagram] Attempting auto-reply to {sender_id}")

        result = send_instagram_message(sender_id, busy_msg, cfg)

        if result.get("success"):
            # Only start the cooldown after a successful send.
            with _REPLY_LOCK:
                _LAST_REPLY[sender_id] = now
            print(f"[Instagram] Auto-reply sent successfully to {sender_id}")
        else:
            print(f"[Instagram] Failed to send auto-reply to {sender_id}:")
            print(f"[Instagram] API result: {result}")


# ============================================================
# WEBHOOK HTTP HANDLER
# ============================================================

class InstagramWebhookHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Suppress default HTTP server logs."""
        pass

    # --------------------------------------------------------
    # GET - Meta verification
    # --------------------------------------------------------

    def do_GET(self):

        cfg = _load_config()
        parsed = urlparse(self.path)

        if parsed.path in ("/webhook", "/webhook/"):

            params = parse_qs(parsed.query)

            mode = params.get("hub.mode", [None])[0]
            token = params.get("hub.verify_token", [None])[0]
            challenge = params.get("hub.challenge", [None])[0]
            expected_token = cfg.get("verify_token")

            if (
                mode == "subscribe"
                and token
                and challenge
                and expected_token
                and _safe_equals(token, expected_token)
            ):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(challenge.encode("utf-8"))

                print("[Instagram] Webhook verification challenge passed successfully!")
                return

            # SECURITY: don't print the actual token.
            print("[Instagram] Webhook verification failed: token mismatch or missing.")

            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Verification failed")
            return

        self.send_response(404)
        self.end_headers()

    # --------------------------------------------------------
    # POST - Incoming webhook event
    # --------------------------------------------------------

    def do_POST(self):

        cfg = _load_config()
        parsed = urlparse(self.path)

        if parsed.path not in ("/webhook", "/webhook/"):
            self.send_response(404)
            self.end_headers()
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0

        body = self.rfile.read(content_length)

        # ----------------------------------------------------
        # HMAC verification
        # ----------------------------------------------------

        app_secret = cfg.get("app_secret")

        if app_secret:

            signature_header = self.headers.get("X-Hub-Signature-256", "")

            if not signature_header.startswith("sha256="):
                print("[Instagram] Missing webhook signature.")
                self.send_response(401)
                self.end_headers()
                return

            expected = hmac.new(
                app_secret.encode("utf-8"),
                body,
                hashlib.sha256
            ).hexdigest()

            actual = signature_header[len("sha256="):]

            if not _safe_equals(expected, actual):
                print("[Instagram] Invalid HMAC signature!")
                self.send_response(401)
                self.end_headers()
                return

        # ----------------------------------------------------
        # Acknowledge Meta immediately
        # ----------------------------------------------------

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"EVENT_RECEIVED")

        # ----------------------------------------------------
        # Parse webhook
        # ----------------------------------------------------

        try:
            payload = json.loads(body.decode("utf-8"))

            if cfg.get("debug_payload"):
                print("\n[Instagram] ===== WEBHOOK PAYLOAD =====")
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                print("[Instagram] ===========================\n")

            msgs = _extract_messages(payload)

            if msgs:
                print(f"[Instagram] Received {len(msgs)} message(s)")
                _handle_incoming_messages(msgs, cfg)
            else:
                print("[Instagram] No text DM messages were extracted from this webhook.")

        except json.JSONDecodeError as e:
            print(f"[Instagram] Invalid JSON payload: {e}")

        except Exception as e:
            print(f"[Instagram] Error processing webhook payload: {e}")


# ============================================================
# SERVER START / STOP
# ============================================================

def _start_webhook(cfg: dict) -> bool:

    global _SERVER
    global _SERVER_THREAD

    if _SERVER is not None:
        return True

    host = cfg.get("webhook_host", "0.0.0.0")
    port = int(cfg.get("webhook_port", 8080))

    try:
        _SERVER = ThreadingHTTPServer((host, port), InstagramWebhookHandler)
        _SERVER.daemon_threads = True

        _SERVER_THREAD = threading.Thread(
            target=_SERVER.serve_forever,
            daemon=True
        )
        _SERVER_THREAD.start()

        print(f"[Instagram] Webhook server started on {host}:{port}")
        return True

    except Exception as e:
        print(f"[Instagram] Failed to start webhook server: {e}")

        _SERVER = None
        _SERVER_THREAD = None
        return False


def _stop_webhook() -> bool:

    global _SERVER
    global _SERVER_THREAD

    if _SERVER is None:
        return True

    try:
        _SERVER.shutdown()
        _SERVER.server_close()

        _SERVER = None
        _SERVER_THREAD = None

        print("[Instagram] Webhook server stopped.")
        return True

    except Exception as e:
        print(f"[Instagram] Error stopping webhook server: {e}")
        return False


# ============================================================
# AUTO START
# ============================================================

_initial_cfg = _load_config()

if _initial_cfg.get("enabled") and _initial_cfg.get("verify_token"):
    _start_webhook(_initial_cfg)


# ============================================================
# NEXUS PLUGIN ENTRY POINT
# ============================================================

def run(data: dict = None) -> dict:
    """
    NEXUS Instagram connector.

    Supported actions:
        status, enable, disable, set_busy, clear_busy,
        start_webhook, stop_webhook, send_test
    """

    data = data or {}
    action = data.get("action", "status")
    message = data.get("message")

    cfg = _load_config()

    # STATUS
    if action == "status":
        return {
            "status": "success",
            "enabled": cfg.get("enabled", False),
            "busy_mode": cfg.get("busy_mode", False),
            "busy_message": cfg.get("busy_message"),
            "webhook_running": _SERVER is not None,
            "webhook_host": cfg.get("webhook_host"),
            "webhook_port": cfg.get("webhook_port"),
            "has_access_token": bool(cfg.get("access_token")),
            "has_verify_token": bool(cfg.get("verify_token")),
            "has_app_secret": bool(cfg.get("app_secret"))
        }

    # ENABLE
    elif action == "enable":
        cfg["enabled"] = True
        _save_config(cfg)

        if _SERVER is None and cfg.get("verify_token"):
            _start_webhook(cfg)

        return {"status": "success", "message": "Instagram connector enabled"}

    # DISABLE
    elif action == "disable":
        cfg["enabled"] = False
        _save_config(cfg)
        _stop_webhook()

        return {"status": "success", "message": "Instagram connector disabled"}

    # SET BUSY
    elif action == "set_busy":
        cfg["busy_mode"] = True

        if message:
            cfg["busy_message"] = message

        _save_config(cfg)

        return {
            "status": "success",
            "busy_mode": True,
            "busy_message": cfg.get("busy_message")
        }

    # CLEAR BUSY
    elif action == "clear_busy":
        cfg["busy_mode"] = False
        _save_config(cfg)

        return {"status": "success", "busy_mode": False}

    # START WEBHOOK
    elif action == "start_webhook":
        success = _start_webhook(cfg)

        return {
            "status": "success" if success else "error",
            "webhook_running": _SERVER is not None
        }

    # STOP WEBHOOK
    elif action == "stop_webhook":
        success = _stop_webhook()

        return {
            "status": "success" if success else "error",
            "webhook_running": _SERVER is not None
        }

    # SEND TEST
    elif action == "send_test":
        recipient = cfg.get("test_recipient_id")

        if not recipient:
            return {
                "status": "error",
                "message": "Missing test_recipient_id in config/instagram.json"
            }

        test_text = message or "Test auto-reply from Nexus Instagram Plugin"

        result = send_instagram_message(recipient, test_text, cfg)

        return {
            "status": "success" if result.get("success") else "error",
            "result": result
        }
    return {"status": "error", "message": f"Unknown action '{action}'"}