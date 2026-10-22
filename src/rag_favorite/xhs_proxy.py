"""Persistent XHS node rotation, owned exclusively by the session service.

The service serializes browser reads and complete media bundles. A node can
therefore change only between operations, including the 80th completed bundle.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .config import ConfigError
from .xhs_private import atomic_private_json, private_directory, private_json
from .xhs_session_policy import failure


class RequiredProxy(urllib.request.ProxyHandler):
    """Explicit loopback proxy; ambient NO_PROXY must never bypass this route."""

    def __init__(self, proxy, on_request=None):
        self.on_request = on_request
        p = urlsplit(proxy)
        if (
            p.scheme != "http"
            or p.hostname != "127.0.0.1"
            or not p.port
            or p.username
            or p.password
            or p.path
            or p.query
            or p.fragment
        ):
            raise ConfigError("XHS requires its managed loopback proxy.")
        super().__init__({"http": proxy, "https": proxy})

    def proxy_open(self, req, proxy, type):
        if self.on_request:
            self.on_request()
        original = req.type
        req.set_proxy(urlsplit(proxy).netloc, "http")
        if original in {"http", "https"}:
            return
        raise ConfigError("Unsupported XHS proxy request.")


def node_outbound(node):
    params = node["parameters"]

    def get(key, default=""):
        return params.get(key, [default])[0]

    address = ipaddress.ip_address(node["address"])
    if (
        not address.is_global
        or get("type") != "ws"
        or get("security") != "tls"
        or not get("sni")
        or get("host") != get("sni")
        or not 1 <= int(node["port"]) <= 65535
    ):
        raise ConfigError("Unsupported XHS node configuration.")
    tls = {
        "serverName": get("sni"),
        "allowInsecure": False,
        "fingerprint": get("fp", "chrome"),
    }
    if get("alpn"):
        tls["alpn"] = get("alpn").split(",")
    return {
        "protocol": "vless",
        "mux": {"enabled": False},
        "settings": {
            "vnext": [
                {
                    "address": str(address),
                    "port": int(node["port"]),
                    "users": [
                        {"id": str(UUID(node["credential"])), "encryption": "none"}
                    ],
                }
            ]
        },
        "streamSettings": {
            "network": "ws",
            "security": "tls",
            "tlsSettings": tls,
            "wsSettings": {"path": get("path", "/"), "headers": {"Host": get("host")}},
        },
    }


class ProxyPool:
    def __init__(self, root, config_path, *, clock=time.time):
        self.root, self.config_path, self.clock = root, Path(config_path), clock
        private_directory(root)
        self.config = private_json(self.config_path)
        self.binary = Path(self.config["binary"])
        self.port = self.config.get("port", 18998)
        self.batch_size = self.config.get("batch_size", 80)
        if (
            type(self.batch_size) is not int
            or not 1 <= self.batch_size <= 1000
            or type(self.port) is not int
            or not 1024 <= self.port <= 65535
        ):
            raise ConfigError("Invalid XHS proxy batch size or port.")
        nodes = private_json(Path(self.config["nodes_file"]))["nodes"]
        self.nodes = {}
        for node in nodes:
            outbound = node_outbound(node)
            ident = hashlib.sha256(
                json.dumps(outbound, sort_keys=True).encode()
            ).hexdigest()
            self.nodes.setdefault(ident, outbound)
        if not self.nodes:
            raise ConfigError("No supported XHS proxy nodes.")
        self.ids = list(self.nodes)
        self.path = root / "proxy-state.json"
        self.data = (
            private_json(self.path)
            if self.path.exists()
            else {
                "current": self.ids[0],
                "generation": 1,
                "completed": 0,
                "total_completed": 0,
                "counted": {},
                "failed": {},
                "round": 1,
                "first": self.ids[0],
                "rechecking_first": False,
                "cooldown_until": 0,
                "reason": "initial",
                "paused": False,
            }
        )
        # Subscription changes never erase the ledger or outstanding cooldown.
        if self.data["current"] not in self.nodes:
            self.select(self.ids[0], "subscription_changed")
        self.process = None
        self.running_generation = None
        # Route is deliberately not restored: production always starts in pool.
        self.route_mode = "pool"
        self.running_route = None
        self.data.setdefault("network_failures", 0)
        self.data.setdefault("backup_used", False)
        self.data.setdefault("node_validated_generation", None)
        self.save()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def save(self):
        atomic_private_json(self.path, self.data)

    def status(self):
        return {
            "enabled": True,
            "node": self.ids.index(self.data["current"]) + 1,
            "nodes": len(self.ids),
            "generation": self.data["generation"],
            "completed": self.data["completed"],
            "batch_size": self.batch_size,
            "total_completed": self.data["total_completed"],
            "failed_nodes": len(set(self.data["failed"]) & set(self.ids)),
            "round": self.data["round"],
            "reason": self.data["reason"],
            "cooldown_until": self.data["cooldown_until"],
            "paused": self.data["paused"],
            "route": self.route_mode,
            "network_failures": self.data["network_failures"],
            "backup_used": self.data["backup_used"],
            "ready": self.process is not None
            and self.process.poll() is None
            and self.running_generation == self.data["generation"]
            and self.running_route == self.route_mode,
        }

    def set_route(self, mode):
        if mode not in {"pool", "direct_diagnostic"}:
            raise ConfigError("Invalid XHS route.")
        if mode != self.route_mode:
            self.stop()
            self.route_mode = mode

    def outbound(self):
        if self.route_mode == "direct_diagnostic":
            return {"protocol": "freedom", "settings": {}}
        return self.nodes[self.data["current"]]

    def select(self, ident, reason):
        self.data.update(
            current=ident,
            completed=0,
            generation=self.data["generation"] + 1,
            reason=reason,
        )
        self.save()

    def next_healthy(self):
        index = self.ids.index(self.data["current"])
        return next(
            (
                self.ids[(index + n) % len(self.ids)]
                for n in range(1, len(self.ids) + 1)
                if self.ids[(index + n) % len(self.ids)] not in self.data["failed"]
            ),
            None,
        )

    def before_request(self, *, diagnostic=False):
        if diagnostic:
            return  # Explicit bounded check; never rotates or updates the ledger.
        if self.route_mode != "pool":
            raise failure("XHS_ACCEPTANCE_PAUSED", delay=60)
        if self.data["paused"]:
            raise failure("XHS_PROXY_PAUSED", delay=30)
        remaining = self.data["cooldown_until"] - self.clock()
        if remaining > 0:
            raise failure("XHS_PROXY_COOLDOWN", delay=remaining + 1)
        if self.data["cooldown_until"]:
            self.data["cooldown_until"] = 0
            self.save()
        if self.data["completed"] >= self.batch_size:
            self.select(self.next_healthy() or self.data["current"], "batch_complete")
            self.data.update(network_failures=0, backup_used=False)
            self.save()

    def before_download(self):
        # A deployment can admit five, then twenty, real downloads without racing
        # the worker. Updating this private limit takes effect on the next call.
        limit = private_json(self.config_path).get("acceptance_limit")
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise ConfigError("Invalid XHS acceptance limit.")
            if self.data["total_completed"] >= limit:
                raise failure("XHS_ACCEPTANCE_PAUSED", delay=30)

    def complete(self, key, generation):
        if key in self.data["counted"]:
            return False
        if generation != self.data["generation"]:
            raise failure("XHS_PROXY_STALE_GENERATION", delay=1)
        self.data["counted"][key] = generation
        self.data["completed"] += 1
        self.data["total_completed"] += 1
        self.data.update(network_failures=0, backup_used=False, cooldown_until=0)
        self.save()
        return True

    def reconcile(self, video_root):
        """Recover a crash between the validated receipt and counter commits."""
        recovered = 0
        for path in (video_root / "derived").glob("*/download-bundle.json"):
            receipt = private_json(path)
            if (
                receipt.get("complete")
                and receipt.get("downloaded")
                and receipt.get("generation") == self.data["generation"]
            ):
                recovered += self.complete(receipt["note_id"], receipt["generation"])
        return recovered

    def failed(self, code):
        if code != "XHS_NETWORK_ERROR":
            self.data.update(paused=True, reason=code)
            self.save()
            return
        if self.data["backup_used"]:
            self.data.update(paused=True, reason="backup_failed")
            self.save()
            return
        self.data["network_failures"] += 1
        attempt = self.data["network_failures"]
        if attempt <= 2:
            self.data.update(
                cooldown_until=self.clock() + (60, 300)[attempt - 1], reason=code
            )
            self.save()
            return
        self.data["failed"][self.data["current"]] = code
        next_node = self.next_healthy()
        if next_node and next_node != self.data["current"]:
            self.data.update(backup_used=True, cooldown_until=0)
            self.select(next_node, code)
        else:
            self.data.update(paused=True, reason="pool_exhausted")
            self.save()

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
            self.running_generation = None
            self.running_route = None

    def ensure_started(self, *, diagnostic=False):
        self.before_request(diagnostic=diagnostic)
        if (
            self.process is not None
            and self.process.poll() is None
            and self.running_generation == self.data["generation"]
            and self.running_route == self.route_mode
        ):
            return False
        self.stop()
        config = {
            "log": {"access": "none", "loglevel": "none"},
            "inbounds": [
                {
                    "listen": "127.0.0.1",
                    "port": self.port,
                    "protocol": "http",
                    "settings": {},
                }
            ],
            "outbounds": [self.outbound()],
        }
        path = self.root / "xray.private.json"
        atomic_private_json(path, config)
        try:
            with socket.socket() as check:
                # Finished proxy connections can retain TIME_WAIT during a node
                # switch. Match Xray's reusable listener without sharing a live port.
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                check.bind(("127.0.0.1", self.port))
            subprocess.run(
                [str(self.binary), "run", "-test", "-config", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=10,
            )
            self.process = subprocess.Popen(
                [str(self.binary), "run", "-config", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5
            while self.process.poll() is None and time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", self.port), 0.1):
                        self.running_generation = self.data["generation"]
                        self.running_route = self.route_mode
                        return True
                except OSError:
                    time.sleep(0.05)
        except (OSError, subprocess.SubprocessError):
            pass
        self.stop()
        raise failure("XHS_PROXY_UNAVAILABLE", delay=30)
