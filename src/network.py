"""Network management helpers for Klarfunk Box (NetworkManager via nmcli)."""

from __future__ import annotations

import logging
import socket
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Result[T, E]:
    """Simple Result container for command outcomes."""

    value: T | None
    error: E | None

    def is_ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class CommandResult:
    """Raw command result."""

    returncode: int
    stdout: str
    stderr: str
    error: str | None


@dataclass(frozen=True)
class ConnectivityStatus:
    """Connectivity status summary."""

    is_connected: bool
    connectivity: str
    reason: str


@dataclass(frozen=True)
class ActiveWifiInfo:
    """Active WiFi connection details."""

    ssid: str
    device: str
    signal: int | None
    ipv4_address: str | None
    gateway: str | None
    dns_servers: tuple[str, ...]


@dataclass(frozen=True)
class SavedWifiNetwork:
    """Saved WiFi network details."""

    name: str
    ssid: str
    security: str
    is_active: bool


class NetworkManager:
    """NetworkManager wrapper using nmcli."""

    def __init__(
        self,
        nmcli_path: str,
        command_timeout_seconds: float,
        connect_timeout_seconds: float,
        internet_check_hosts: tuple[str, ...],
        internet_check_port: int,
        internet_check_timeout_seconds: float,
    ) -> None:
        if not isinstance(nmcli_path, str) or not nmcli_path.strip():
            msg = "nmcli_path must be a non-empty string"
            raise ValueError(msg)
        if (
            not isinstance(command_timeout_seconds, (int, float))
            or command_timeout_seconds < 0
        ):
            msg = "command_timeout_seconds must be a non-negative number"
            raise ValueError(msg)
        if (
            not isinstance(connect_timeout_seconds, (int, float))
            or connect_timeout_seconds < 0
        ):
            msg = "connect_timeout_seconds must be a non-negative number"
            raise ValueError(msg)
        if (
            not isinstance(internet_check_timeout_seconds, (int, float))
            or internet_check_timeout_seconds < 0
        ):
            msg = "internet_check_timeout_seconds must be a non-negative number"
            raise ValueError(msg)
        if not isinstance(internet_check_hosts, tuple) or not internet_check_hosts:
            msg = "internet_check_hosts must be a non-empty tuple of strings"
            raise ValueError(msg)
        if any(
            not isinstance(host, str) or not host.strip()
            for host in internet_check_hosts
        ):
            msg = "internet_check_hosts must be a non-empty tuple of strings"
            raise ValueError(msg)
        if (
            not isinstance(internet_check_port, int)
            or not 1 <= internet_check_port <= 65535
        ):
            msg = "internet_check_port must be between 1 and 65535"
            raise ValueError(msg)
        self._nmcli_path = nmcli_path
        self._command_timeout_seconds = command_timeout_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._internet_check_hosts = internet_check_hosts
        self._internet_check_port = internet_check_port
        self._internet_check_timeout_seconds = internet_check_timeout_seconds

    def check_connectivity(self) -> ConnectivityStatus:
        """Return connectivity status using nmcli and socket probe fallback."""
        result = self._run_nmcli(
            ("-t", "-f", "CONNECTIVITY", "general"),
            timeout_seconds=self._command_timeout_seconds,
        )
        if result.error is None and result.returncode == 0:
            status = result.stdout.strip()
            is_connected = status == "full"
            reason = "nmcli reported full connectivity" if is_connected else status
            return ConnectivityStatus(
                is_connected=is_connected,
                connectivity=status,
                reason=reason,
            )

        socket_ok = self._check_hosts()
        reason = (
            "socket probe succeeded"
            if socket_ok
            else (result.error or "nmcli unavailable")
        )
        return ConnectivityStatus(
            is_connected=socket_ok,
            connectivity="unknown",
            reason=reason,
        )

    def get_active_wifi(self) -> Result[ActiveWifiInfo | None, str]:
        """Return active WiFi details, or None if not connected."""
        result = self._run_nmcli(
            (
                "-m",
                "multiline",
                "-t",
                "-f",
                "ACTIVE,SSID,DEVICE,SIGNAL",
                "dev",
                "wifi",
            ),
            timeout_seconds=self._command_timeout_seconds,
        )
        if result.error is not None or result.returncode != 0:
            msg = result.error or result.stderr.strip() or "nmcli dev wifi failed"
            return Result(value=None, error=msg)

        records = _parse_multiline_records(result.stdout)
        active = next(
            (record for record in records if record.get("ACTIVE") == "yes"),
            None,
        )
        if active is None:
            return Result(value=None, error=None)

        signal = _to_int(active.get("SIGNAL", ""))
        device = active.get("DEVICE", "")
        ip_record = self._get_ip_details(device)
        dns_values = _get_all_prefixed(ip_record, "IP4.DNS")
        dns_servers = tuple(item for value in dns_values for item in _split_dns(value))
        info = ActiveWifiInfo(
            ssid=active.get("SSID", ""),
            device=device,
            signal=signal,
            ipv4_address=_get_first_prefixed(ip_record, "IP4.ADDRESS") or None,
            gateway=_get_first_prefixed(ip_record, "IP4.GATEWAY") or None,
            dns_servers=dns_servers,
        )
        return Result(value=info, error=None)

    def list_saved_wifi(self) -> Result[tuple[SavedWifiNetwork, ...], str]:
        """List saved WiFi connections with basic details."""
        result = self._run_nmcli(
            (
                "-m",
                "multiline",
                "-t",
                "-f",
                "NAME,TYPE,802-11-wireless.ssid,802-11-wireless-security.key-mgmt",
                "connection",
                "show",
            ),
            timeout_seconds=self._command_timeout_seconds,
        )
        if result.error is not None or result.returncode != 0:
            msg = (
                result.error or result.stderr.strip() or "nmcli connection show failed"
            )
            return Result(value=None, error=msg)

        active_names = self._get_active_wifi_names()
        records = _parse_multiline_records(result.stdout)
        wifi_records = tuple(
            record for record in records if record.get("TYPE") == "wifi"
        )
        networks = tuple(
            SavedWifiNetwork(
                name=record.get("NAME", ""),
                ssid=record.get("802-11-wireless.ssid", ""),
                security=record.get("802-11-wireless-security.key-mgmt", "") or "open",
                is_active=record.get("NAME", "") in active_names,
            )
            for record in wifi_records
        )
        return Result(value=networks, error=None)

    def connect_to_saved_wifi(self, name: str) -> Result[bool, str]:
        """Connect to a saved WiFi connection by name."""
        result = self._run_nmcli(
            ("connection", "up", name),
            timeout_seconds=self._connect_timeout_seconds,
        )
        if result.error is not None or result.returncode != 0:
            msg = result.error or result.stderr.strip() or "nmcli connection up failed"
            return Result(value=None, error=msg)
        return Result(value=True, error=None)

    def _get_active_wifi_names(self) -> tuple[str, ...]:
        result = self._run_nmcli(
            (
                "-m",
                "multiline",
                "-t",
                "-f",
                "NAME,TYPE",
                "connection",
                "show",
                "--active",
            ),
            timeout_seconds=self._command_timeout_seconds,
        )
        if result.error is not None or result.returncode != 0:
            return ()

        records = _parse_multiline_records(result.stdout)
        active_wifi = tuple(
            record.get("NAME", "") for record in records if record.get("TYPE") == "wifi"
        )
        return tuple(name for name in active_wifi if name)

    def _get_ip_details(self, device: str) -> dict[str, str]:
        if not device:
            return {}

        result = self._run_nmcli(
            (
                "-m",
                "multiline",
                "-t",
                "-f",
                "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
                "dev",
                "show",
                device,
            ),
            timeout_seconds=self._command_timeout_seconds,
        )
        if result.error is not None or result.returncode != 0:
            return {}

        records = _parse_multiline_records(result.stdout)
        if not records:
            return {}

        return records[0]

    def _run_nmcli(
        self, args: tuple[str, ...], timeout_seconds: float
    ) -> CommandResult:
        cmd = (self._nmcli_path, *args)
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except FileNotFoundError:
            msg = f"nmcli not found at '{self._nmcli_path}'"
            logger.warning(msg)
            return CommandResult(1, "", "", msg)
        except subprocess.TimeoutExpired:
            msg = "nmcli timed out"
            logger.warning(msg)
            return CommandResult(1, "", "", msg)

        return CommandResult(
            completed.returncode,
            completed.stdout.strip(),
            completed.stderr.strip(),
            None,
        )

    def _check_hosts(self) -> bool:
        if not self._internet_check_hosts:
            return False

        for host in self._internet_check_hosts:
            try:
                with socket.create_connection(
                    (host, self._internet_check_port),
                    timeout=self._internet_check_timeout_seconds,
                ):
                    return True
            except OSError:
                continue
        return False


def _parse_multiline_records(output: str) -> tuple[dict[str, str], ...]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}

    for line in output.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue

        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        current = {**current, key: value}

    if current:
        records.append(current)

    return tuple(records)


def _get_first_prefixed(record: dict[str, str], prefix: str) -> str:
    if prefix in record:
        return record[prefix]
    prefix_key = f"{prefix}["
    for key, value in record.items():
        if key.startswith(prefix_key):
            return value
    return ""


def _get_all_prefixed(record: dict[str, str], prefix: str) -> tuple[str, ...]:
    values = tuple(
        value
        for key, value in record.items()
        if key == prefix or key.startswith(f"{prefix}[")
    )
    return values


def _split_dns(value: str) -> tuple[str, ...]:
    cleaned = value.strip()
    if not cleaned:
        return ()

    if "," in cleaned:
        parts = tuple(part.strip() for part in cleaned.split(","))
    else:
        parts = tuple(part.strip() for part in cleaned.split())

    return tuple(part for part in parts if part)


def _to_int(value: str) -> int | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if not cleaned.isdigit():
        return None
    return int(cleaned)
