"""Tests for network management — nmcli parsing and subprocess mocking."""

from __future__ import annotations

import subprocess

import pytest
from pytest_mock import MockerFixture

from src.network import (
    NetworkManager,
    Result,
    _get_all_prefixed,
    _get_first_prefixed,
    _parse_multiline_records,
    _split_dns,
    _to_int,
)

# ---------------------------------------------------------------------------
# Unit tests for pure parsing functions
# ---------------------------------------------------------------------------


class TestParseMultilineRecords:
    def test_single_record(self) -> None:
        output = "GENERAL.DEVICE:wlan0\nGENERAL.TYPE:wifi"
        records = _parse_multiline_records(output)
        assert len(records) == 1
        assert records[0]["GENERAL.DEVICE"] == "wlan0"
        assert records[0]["GENERAL.TYPE"] == "wifi"

    def test_multiple_records(self) -> None:
        output = "NAME:net1\nSSID:ssid1\n\nNAME:net2\nSSID:ssid2"
        records = _parse_multiline_records(output)
        assert len(records) == 2
        assert records[0]["NAME"] == "net1"
        assert records[1]["NAME"] == "net2"

    def test_empty_output(self) -> None:
        records = _parse_multiline_records("")
        assert records == ()

    def test_colon_in_value(self) -> None:
        output = "KEY:value:with:colons"
        records = _parse_multiline_records(output)
        assert records[0]["KEY"] == "value:with:colons"


class TestGetFirstPrefixed:
    def test_finds_matching_key(self) -> None:
        record = {"IP4.ADDRESS[1]": "192.168.1.1/24", "IP4.GATEWAY": "192.168.1.1"}
        assert _get_first_prefixed(record, "IP4.ADDRESS") == "192.168.1.1/24"

    def test_returns_empty_string_when_no_match(self) -> None:
        record = {"IP4.GATEWAY": "192.168.1.1"}
        assert _get_first_prefixed(record, "IP6.ADDRESS") == ""


class TestGetAllPrefixed:
    def test_returns_all_matching(self) -> None:
        record = {
            "IP4.DNS[1]": "8.8.8.8",
            "IP4.DNS[2]": "8.8.4.4",
            "IP4.GATEWAY": "192.168.1.1",
        }
        values = _get_all_prefixed(record, "IP4.DNS")
        assert values == ("8.8.8.8", "8.8.4.4")


class TestSplitDns:
    def test_single_dns(self) -> None:
        assert _split_dns("8.8.8.8") == ("8.8.8.8",)

    def test_comma_separated(self) -> None:
        assert _split_dns("8.8.8.8,8.8.4.4") == ("8.8.8.8", "8.8.4.4")

    def test_space_separated(self) -> None:
        assert _split_dns("8.8.8.8 8.8.4.4") == ("8.8.8.8", "8.8.4.4")

    def test_empty_string(self) -> None:
        assert _split_dns("") == ()

    def test_whitespace_trimmed(self) -> None:
        assert _split_dns("  1.1.1.1  ,  1.0.0.1  ") == ("1.1.1.1", "1.0.0.1")


class TestToInt:
    def test_valid_integer(self) -> None:
        assert _to_int("42") == 42

    def test_invalid_integer(self) -> None:
        assert _to_int("abc") is None

    def test_empty_string(self) -> None:
        assert _to_int("") is None


# ---------------------------------------------------------------------------
# NetworkManager with subprocess mocking
# ---------------------------------------------------------------------------


@pytest.fixture
def network_mgr() -> NetworkManager:
    return NetworkManager(
        nmcli_path="nmcli",
        command_timeout_seconds=5.0,
        connect_timeout_seconds=20.0,
        internet_check_hosts=("1.1.1.1",),
        internet_check_port=53,
        internet_check_timeout_seconds=1.0,
    )


class TestCheckConnectivity:
    def test_connected_via_nmcli(
        self, network_mgr: NetworkManager, mocker: MockerFixture
    ) -> None:
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="full\n", stderr=""
            ),
        )
        status = network_mgr.check_connectivity()
        assert status.is_connected is True
        assert "full" in status.connectivity

    def test_nmcli_not_found_falls_back_to_socket(
        self, network_mgr: NetworkManager, mocker: MockerFixture
    ) -> None:
        mocker.patch("subprocess.run", side_effect=FileNotFoundError("nmcli"))
        mock_socket = mocker.patch("socket.create_connection")
        mock_socket.return_value.__enter__ = lambda s: s
        mock_socket.return_value.__exit__ = lambda s, *a: None

        status = network_mgr.check_connectivity()
        assert status.is_connected is True

    def test_no_connectivity(
        self, network_mgr: NetworkManager, mocker: MockerFixture
    ) -> None:
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="none\n", stderr=""
            ),
        )
        mocker.patch("socket.create_connection", side_effect=OSError("no route"))

        status = network_mgr.check_connectivity()
        assert status.is_connected is False


class TestListSavedWifi:
    def test_parses_saved_networks(
        self, network_mgr: NetworkManager, mocker: MockerFixture
    ) -> None:
        # Multiline format as produced by `nmcli -m multiline -t`
        # list_saved_wifi calls _run_nmcli once, then _get_active_wifi_names
        # which calls _run_nmcli again. We need to mock both.
        list_output = (
            "NAME:Home WiFi\n"
            "TYPE:wifi\n"
            "802-11-wireless.ssid:HomeSSID\n"
            "802-11-wireless-security.key-mgmt:wpa-psk\n"
        )
        active_output = ""  # No active wifi
        mocker.patch(
            "subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=list_output, stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=active_output, stderr=""
                ),
            ],
        )

        result = network_mgr.list_saved_wifi()
        assert result.is_ok()
        networks = result.value
        assert networks is not None
        assert len(networks) == 1
        assert networks[0].name == "Home WiFi"
        assert networks[0].ssid == "HomeSSID"

    def test_nmcli_timeout(
        self, network_mgr: NetworkManager, mocker: MockerFixture
    ) -> None:
        mocker.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="nmcli", timeout=5),
        )

        result = network_mgr.list_saved_wifi()
        assert not result.is_ok()


class TestConnectToSavedWifi:
    def test_connect_success(
        self, network_mgr: NetworkManager, mocker: MockerFixture
    ) -> None:
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        )

        result = network_mgr.connect_to_saved_wifi("Home WiFi")
        assert result.is_ok()

    def test_connect_timeout(
        self, network_mgr: NetworkManager, mocker: MockerFixture
    ) -> None:
        mocker.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="nmcli", timeout=20),
        )

        result = network_mgr.connect_to_saved_wifi("Home WiFi")
        assert not result.is_ok()


class TestResult:
    def test_ok_result(self) -> None:
        result: Result[str, str] = Result(value="data", error=None)
        assert result.is_ok()
        assert result.value == "data"

    def test_error_result(self) -> None:
        result: Result[str, str] = Result(value=None, error="fail")
        assert not result.is_ok()
        assert result.error == "fail"
