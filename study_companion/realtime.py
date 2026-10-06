"""Supabase Realtime WebSocket client for low-latency in-room peer sync.

Bypasses periodic database polling for live status and review ticks:
- Broadcast: review activity ticks (slot, count, duration) relayed to peers in <50ms without DB writes.
- Presence: room member online/studying status and current deck tracking via CRDT.
- Heartbeat: maintains WebSocket channel and detects network drops automatically.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .nicknames import sanitize_display_name

try:
    from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
    from PyQt6.QtWebSockets import QWebSocket
    HAS_QT_WEBSOCKET = True
except ImportError:
    HAS_QT_WEBSOCKET = False

    class QObject:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _Signal:
        def __init__(self) -> None:
            self._slots: list[Callable] = []

        def connect(self, slot: Callable) -> None:
            self._slots.append(slot)

        def disconnect(self, slot: Callable | None = None) -> None:
            if slot is None:
                self._slots.clear()
            elif slot in self._slots:
                self._slots.remove(slot)

        def emit(self, *args: Any) -> None:
            for s in list(self._slots):
                s(*args)

    class _SignalDescriptor:
        def __init__(self) -> None:
            self._name = ""

        def __set_name__(self, owner: Any, name: str) -> None:
            self._name = f"_sig_{name}"

        def __get__(self, instance: Any, owner: Any) -> Any:
            if instance is None:
                return self
            if not hasattr(instance, self._name):
                setattr(instance, self._name, _Signal())
            return getattr(instance, self._name)

    def pyqtSignal(*types: Any) -> Any:  # type: ignore[no-redef]
        return _SignalDescriptor()

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_URL = "wss://uvrnsdknkivtclzlfjxx.supabase.co/realtime/v1/websocket"
HEARTBEAT_INTERVAL_MS = 25000
RECONNECT_BASE_MS = 2000
RECONNECT_MAX_MS = 30000


def apply_review_tick_to_members(
    members: list[dict[str, Any]],
    user_id: str,
    slot: int,
    answers: int = 1,
    time_ms: int = 0,
) -> bool:
    """Apply an in-memory review tick from a peer to the cached member list."""
    for member in members:
        if str(member.get("user_id") or "") == str(user_id):
            member["answer_count"] = max(0, int(member.get("answer_count") or 0)) + answers
            member["activity_known"] = True
            buckets = member.setdefault("activity_buckets", [])
            for b in buckets:
                if isinstance(b, dict) and b.get("slot") == slot:
                    b["answer_count"] = max(0, int(b.get("answer_count") or 0)) + answers
                    b["time_ms"] = max(0, int(b.get("time_ms") or 0)) + time_ms
                    return True
            buckets.append({"slot": slot, "answer_count": answers, "time_ms": time_ms})
            return True
    return False


def _clean_peer_meta(meta: dict[str, Any], user_id: str) -> dict[str, Any]:
    cleaned = dict(meta)
    if "display_name" in cleaned and cleaned["display_name"] is not None:
        cleaned["display_name"] = sanitize_display_name(cleaned["display_name"], user_id)
    return cleaned


def apply_presence_to_members(
    members: list[dict[str, Any]],
    presences: dict[str, dict[str, Any]],
    current_iso: str,
) -> bool:
    """Update member online statuses, deck names, and display names from presence dictionary."""
    changed = False
    for member in members:
        uid = str(member.get("user_id") or "")
        if uid in presences:
            meta = presences[uid]
            if "status" in meta and member.get("status") != meta["status"]:
                member["status"] = meta["status"]
                changed = True
            if "current_deck_name" in meta and member.get("current_deck_name") != meta["current_deck_name"]:
                member["current_deck_name"] = meta["current_deck_name"]
                changed = True
            if "display_name" in meta and meta["display_name"]:
                safe_name = sanitize_display_name(meta["display_name"], uid)
                if member.get("display_name") != safe_name:
                    member["display_name"] = safe_name
                    changed = True
            member["updated_at"] = current_iso
            member["deck_updated_at"] = current_iso
    return changed


class RealtimeClient(QObject):
    """Manages WebSocket connection to Supabase Realtime."""

    connected = pyqtSignal()
    disconnected = pyqtSignal()
    # Emits (user_id, slot, answers, time_ms)
    review_tick_received = pyqtSignal(str, int, int, int)
    # Emits full {user_id: meta_dict} mapping
    presence_changed = pyqtSignal(dict)

    def __init__(
        self,
        base_ws_url: str = DEFAULT_REALTIME_URL,
        apikey: str = "",
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.base_ws_url = base_ws_url.rstrip("/")
        self.apikey = apikey

        self._ref = 0
        self.group_id: str | None = None
        self.user_id: str | None = None
        self._current_topic: str | None = None
        self._channel_joined = False

        self._presence_store: dict[str, dict[str, Any]] = {}
        self._last_presence_meta: dict[str, Any] = {}

        self._reconnect_attempts = 0
        self._ws: Any = None
        self._heartbeat_timer: Any = None
        self._reconnect_timer: Any = None

        if HAS_QT_WEBSOCKET:
            self._ws = QWebSocket()
            self._ws.connected.connect(self._on_connected)
            self._ws.disconnected.connect(self._on_disconnected)
            self._ws.textMessageReceived.connect(self._on_message)
            self._ws.errorOccurred.connect(self._on_error)

            self._heartbeat_timer = QTimer(self)
            self._heartbeat_timer.setInterval(HEARTBEAT_INTERVAL_MS)
            self._heartbeat_timer.timeout.connect(self._send_heartbeat)

            self._reconnect_timer = QTimer(self)
            self._reconnect_timer.setSingleShot(True)
            self._reconnect_timer.timeout.connect(self._connect_socket)

    def is_available(self) -> bool:
        return HAS_QT_WEBSOCKET and self._ws is not None

    def is_connected(self) -> bool:
        if not self.is_available():
            return False
        return self._channel_joined

    def set_credentials(self, base_ws_url: str, apikey: str) -> None:
        self.base_ws_url = base_ws_url.rstrip("/")
        self.apikey = apikey

    def join_room(self, group_id: str, user_id: str) -> None:
        """Connect and join room channel for live broadcast and presence."""
        if not self.is_available():
            return
        if self.group_id == group_id and self.user_id == user_id and self._channel_joined:
            return

        self.group_id = str(group_id)
        self.user_id = str(user_id)
        self._presence_store.clear()

        self._connect_socket()

    def leave_room(self) -> None:
        """Leave current room channel and disconnect."""
        if not self.is_available():
            return
        if self._current_topic and self._channel_joined:
            leave_msg = {
                "topic": self._current_topic,
                "event": "phx_leave",
                "payload": {},
                "ref": str(self._next_ref()),
            }
            self._send_json(leave_msg)
        self._channel_joined = False
        self._current_topic = None
        self.group_id = None
        self._presence_store.clear()
        if self._ws:
            self._ws.close()
        if self._heartbeat_timer:
            self._heartbeat_timer.stop()
        if self._reconnect_timer:
            self._reconnect_timer.stop()

    def update_presence(
        self,
        status: str,
        current_deck_name: str | None = None,
        display_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Publish our current state to all peers in the room."""
        if not self.is_available() or not self.user_id or not self._channel_joined:
            return
        meta = {
            "user_id": self.user_id,
            "status": status,
            "current_deck_name": current_deck_name,
        }
        if display_name:
            meta["display_name"] = sanitize_display_name(display_name, self.user_id)
        if meta == self._last_presence_meta:
            return
        self._last_presence_meta = meta

        # 1. Phoenix presence track
        track_msg = {
            "topic": self._current_topic,
            "event": "presence",
            "payload": {
                "type": "presence",
                "event": "track",
                "payload": meta,
            },
            "ref": str(self._next_ref()),
        }
        self._send_json(track_msg)

        # 2. Instant broadcast to peers
        self._broadcast_member_state(meta)

    def _broadcast_member_state(self, meta: dict[str, Any]) -> None:
        if not self._channel_joined or not self._current_topic:
            return
        msg = {
            "topic": self._current_topic,
            "event": "broadcast",
            "payload": {
                "type": "broadcast",
                "event": "member_state",
                "payload": meta,
            },
            "ref": str(self._next_ref()),
        }
        self._send_json(msg)

    def broadcast_review_tick(self, slot: int, count: int = 1, time_ms: int = 0) -> None:
        """Broadcast an answered card review tick to peers in real time."""
        if not self.is_available() or not self.user_id or not self._channel_joined:
            return
        msg = {
            "topic": self._current_topic,
            "event": "broadcast",
            "payload": {
                "type": "broadcast",
                "event": "review_tick",
                "payload": {
                    "user_id": self.user_id,
                    "slot": int(slot),
                    "answers": int(count),
                    "time_ms": int(time_ms),
                },
            },
            "ref": str(self._next_ref()),
        }
        self._send_json(msg)

    def current_presences(self) -> dict[str, dict[str, Any]]:
        """Return cached presence states for all members in the room."""
        return dict(self._presence_store)

    # --- Private Connection & Protocol Methods ---

    def _next_ref(self) -> int:
        self._ref += 1
        return self._ref

    def _connect_socket(self) -> None:
        if not self.is_available() or not self.apikey:
            return
        from PyQt6.QtCore import QUrl
        url_str = f"{self.base_ws_url}?apikey={self.apikey}&vsn=1.0.0"
        self._channel_joined = False
        self._ws.open(QUrl(url_str))

    def _on_connected(self) -> None:
        self._reconnect_attempts = 0
        if self._heartbeat_timer:
            self._heartbeat_timer.start()
        # Join room channel
        if self.group_id and self.user_id:
            topic = f"realtime:room:{self.group_id}"
            self._current_topic = topic
            join_msg = {
                "topic": topic,
                "event": "phx_join",
                "payload": {
                    "config": {
                        "broadcast": {"self": False},
                        "presence": {"key": self.user_id},
                    }
                },
                "ref": str(self._next_ref()),
            }
            self._send_json(join_msg)

    def _on_disconnected(self) -> None:
        self._channel_joined = False
        if self._heartbeat_timer:
            self._heartbeat_timer.stop()
        self.disconnected.emit()
        self._schedule_reconnect()

    def _on_error(self, error: Any) -> None:
        logger.warning("Supabase Realtime WebSocket error: %s (%s)", error, self._ws.errorString() if self._ws else "")
        self._channel_joined = False
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if not self.group_id or not self._reconnect_timer:
            return
        self._reconnect_attempts += 1
        delay = min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * (2 ** min(self._reconnect_attempts - 1, 4)))
        self._reconnect_timer.start(delay)

    def _send_heartbeat(self) -> None:
        hb_msg = {
            "topic": "phoenix",
            "event": "heartbeat",
            "payload": {},
            "ref": str(self._next_ref()),
        }
        self._send_json(hb_msg)

    def _send_json(self, payload: dict) -> bool:
        if not self._ws or not self._ws.isValid():
            return False
        try:
            return bool(self._ws.sendTextMessage(json.dumps(payload)))
        except Exception as err:
            logger.debug("Realtime send failed: %s", err)
            return False

    def _on_message(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except (ValueError, TypeError):
            return

        event = msg.get("event")
        payload = msg.get("payload") or {}

        # Channel join confirmed
        if event == "phx_reply" and self._current_topic and msg.get("topic") == self._current_topic:
            if payload.get("status") == "ok":
                self._channel_joined = True
                self.connected.emit()
                # Request existing peer states with a ping
                ping_msg = {
                    "topic": self._current_topic,
                    "event": "broadcast",
                    "payload": {
                        "type": "broadcast",
                        "event": "room_ping",
                        "payload": {"user_id": self.user_id},
                    },
                    "ref": str(self._next_ref()),
                }
                self._send_json(ping_msg)
                if self._last_presence_meta:
                    meta = dict(self._last_presence_meta)
                    self._last_presence_meta = {}  # force send
                    self.update_presence(**meta)
            return

        # Incoming broadcast
        if event == "broadcast":
            inner_event = payload.get("event")
            inner_payload = payload.get("payload") or {}
            if inner_event == "review_tick":
                user_id = str(inner_payload.get("user_id") or "")
                if user_id and user_id != self.user_id:
                    slot = int(inner_payload.get("slot", 0))
                    answers = int(inner_payload.get("answers", 1))
                    time_ms = int(inner_payload.get("time_ms", 0))
                    self.review_tick_received.emit(user_id, slot, answers, time_ms)
            elif inner_event == "member_state":
                user_id = str(inner_payload.get("user_id") or "")
                if user_id and user_id != self.user_id:
                    self._presence_store[user_id] = _clean_peer_meta(inner_payload, user_id)
                    self.presence_changed.emit(dict(self._presence_store))
            elif inner_event == "room_ping":
                user_id = str(inner_payload.get("user_id") or "")
                if user_id and user_id != self.user_id:
                    if self._last_presence_meta:
                        self._broadcast_member_state(self._last_presence_meta)
            return

        # Presence initial state
        if event == "presence_state":
            self._presence_store.clear()
            for key, val in payload.items():
                metas = val.get("metas") or []
                if metas:
                    self._presence_store[key] = _clean_peer_meta(metas[0], str(key))
            self.presence_changed.emit(dict(self._presence_store))
            return

        # Presence diff (joins / leaves)
        if event == "presence_diff":
            joins = payload.get("joins") or {}
            leaves = payload.get("leaves") or {}
            for key in leaves:
                self._presence_store.pop(key, None)
            for key, val in joins.items():
                metas = val.get("metas") or []
                if metas:
                    self._presence_store[key] = _clean_peer_meta(metas[0], str(key))
            self.presence_changed.emit(dict(self._presence_store))
            return
