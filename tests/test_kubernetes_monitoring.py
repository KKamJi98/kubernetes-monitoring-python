import os
import sys
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from rich.live import Live
from rich.text import Text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))
import kubernetes_monitoring


def test_main_function_exists():
    """Test that main function exists and is callable"""
    assert callable(kubernetes_monitoring.main)


def test_node_group_label_constant():
    """Test that NODE_GROUP_LABEL constant is defined"""
    assert hasattr(kubernetes_monitoring, "NODE_GROUP_LABEL")
    assert isinstance(kubernetes_monitoring.NODE_GROUP_LABEL, str)


def test_parse_cpu_to_millicores():
    """CPU 문자열 파싱 검증"""
    assert kubernetes_monitoring._parse_cpu_to_millicores("500m") == 500
    assert kubernetes_monitoring._parse_cpu_to_millicores("1") == 1000
    assert kubernetes_monitoring._parse_cpu_to_millicores("250n") == 0
    assert kubernetes_monitoring._parse_cpu_to_millicores("") == 0


def test_parse_memory_to_bytes():
    """Memory 문자열 파싱 검증"""
    assert kubernetes_monitoring._parse_memory_to_bytes("50Mi") == 50 * 1024**2
    assert kubernetes_monitoring._parse_memory_to_bytes("2Gi") == 2 * 1024**3
    assert kubernetes_monitoring._parse_memory_to_bytes("1000") == 1000
    assert kubernetes_monitoring._parse_memory_to_bytes("unknown") == 0


@patch("kubernetes_monitoring.subprocess.run")
def test_get_kubectl_top_pod_all_namespaces_success(mock_run):
    """kubectl top pod 전체 namespace 파싱 검증"""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="default pod-a 10m 20Mi\nkube-system pod-b 5m 10Mi\n",
        stderr="",
    )

    metrics, error, command = kubernetes_monitoring._get_kubectl_top_pod(None)

    assert error is None
    assert metrics == [
        ("default", "pod-a", "10m", "20Mi"),
        ("kube-system", "pod-b", "5m", "10Mi"),
    ]
    assert command == "kubectl top pod --no-headers -A"


@patch("kubernetes_monitoring.subprocess.run")
def test_get_kubectl_top_pod_namespace_success(mock_run):
    """kubectl top pod namespace 지정 파싱 검증"""
    mock_run.return_value = MagicMock(
        returncode=0, stdout="pod-a 15m 30Mi\n", stderr=""
    )

    metrics, error, command = kubernetes_monitoring._get_kubectl_top_pod("default")

    assert error is None
    assert metrics == [("default", "pod-a", "15m", "30Mi")]
    assert command == "kubectl top pod --no-headers -n default"


@patch("kubernetes_monitoring.subprocess.run")
def test_get_kubectl_top_pod_failure(mock_run):
    """kubectl top pod 실패 처리 검증"""
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")

    metrics, error, command = kubernetes_monitoring._get_kubectl_top_pod(None)

    assert metrics == []
    assert error == "boom"
    assert command == "kubectl top pod --no-headers -A"


@patch("kubernetes_monitoring.config")
def test_reload_kube_config_success(mock_config):
    """Test successful kube config reloading"""
    mock_config.load_kube_config.return_value = None

    # Should return True on success
    result = kubernetes_monitoring.reload_kube_config_if_changed(force=True)
    assert result is True
    mock_config.load_kube_config.assert_called_once()


@patch("kubernetes_monitoring.config")
def test_reload_kube_config_failure(mock_config):
    """Test kube config reloading failure"""
    mock_config.load_kube_config.side_effect = Exception("Config error")

    result = kubernetes_monitoring.reload_kube_config_if_changed(force=True)
    assert result is False


@patch("kubernetes_monitoring.Prompt.ask")
@patch("kubernetes_monitoring.client")
def test_choose_namespace_success(mock_client, mock_prompt):
    """Test successful namespace selection"""
    # Mock the namespace list
    mock_ns = MagicMock()
    mock_ns.metadata.name = "test-namespace"

    mock_ns_list = MagicMock()
    mock_ns_list.items = [mock_ns]

    mock_v1 = MagicMock()
    mock_v1.list_namespace.return_value = mock_ns_list
    mock_client.CoreV1Api.return_value = mock_v1

    # Mock user input (empty string for default/all namespaces)
    mock_prompt.return_value = ""

    # This should not raise an exception
    kubernetes_monitoring.choose_namespace()


@patch("kubernetes_monitoring.client")
def test_choose_namespace_failure(mock_client):
    """Test namespace selection failure"""
    mock_v1 = MagicMock()
    mock_v1.list_namespace.side_effect = Exception("API error")
    mock_client.CoreV1Api.return_value = mock_v1

    kubernetes_monitoring.choose_namespace()


def test_save_markdown_snapshot_success(tmp_path, monkeypatch):
    """Slack 스냅샷 저장 성공 시 tmp 경로에 파일 생성."""
    monkeypatch.setattr(kubernetes_monitoring, "SNAPSHOT_EXPORT_DIR", tmp_path)
    path = kubernetes_monitoring._save_markdown_snapshot("hello world")
    assert path.parent == tmp_path
    assert path.read_text(encoding="utf-8") == "hello world\n"


def test_save_markdown_snapshot_permission_error(monkeypatch):
    """디렉터리 생성 실패 시 SnapshotSaveError 발생."""

    def fail_mkdir(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("pathlib.Path.mkdir", fail_mkdir)

    with pytest.raises(kubernetes_monitoring.SnapshotSaveError):
        kubernetes_monitoring._save_markdown_snapshot("data")


class _DummyLive:
    def __init__(self, console: Console) -> None:
        self.console = console


def _helper_fake_save(path: Path, markdown: str) -> Path:
    path.write_text(markdown)
    return path


def test_handle_snapshot_command_messages(monkeypatch, tmp_path):
    """저장 명령 처리 시 사용자 입력을 메시지에 포함한다."""
    kubernetes_monitoring._clear_input_display()
    invalid_console = Console(record=True)
    invalid_live = _DummyLive(invalid_console)
    mock_tracker = MagicMock()
    mock_tracker.latest_snapshot = None
    mock_tracker.latest_structured_data = None
    kubernetes_monitoring._handle_snapshot_command(
        invalid_live, mock_tracker, "invalid"
    )
    text_output = invalid_console.export_text()
    assert "입력 'invalid' 은(는) 지원하지 않는 명령입니다." in text_output

    # Success path message
    success_console = Console(record=True)
    success_live = _DummyLive(success_console)

    saved_path = tmp_path / "snapshot.md"
    fake_save_with_path = partial(_helper_fake_save, saved_path)

    monkeypatch.setattr(
        kubernetes_monitoring,
        "_save_markdown_snapshot",
        fake_save_with_path,
        raising=False,
    )
    mock_tracker.latest_snapshot = "some data"
    kubernetes_monitoring._handle_snapshot_command(success_live, mock_tracker, ":save")
    text_output = success_console.export_text()
    assert "입력 ':save' 처리 성공" in text_output

    assert "Slack Markdown 스냅샷 저장 완료" in text_output
    assert str(saved_path) in text_output


def test_live_frame_tracker_updates_sections():
    """LiveFrameTracker가 섹션별 프레임 키를 독립적으로 추적한다."""
    kubernetes_monitoring._clear_input_display()
    console = Console(record=True)
    with Live(console=console, auto_refresh=False) as live:
        tracker = kubernetes_monitoring.LiveFrameTracker(live)
        frame_key = ("test", ("frame",))
        renderable = kubernetes_monitoring._compose_group("cmd-1", Text("body-1"))
        tracker.update(frame_key, renderable, snapshot_markdown=None, input_state="")
        assert tracker.section_frames["input"] == ("input", ("hidden", ""))
        assert tracker.section_frames["body"] == frame_key
        assert tracker.section_frames["footer"] is None

        renderable_updated = kubernetes_monitoring._compose_group(
            "cmd-2", Text("body-1")
        )
        tracker.update(
            frame_key, renderable_updated, snapshot_markdown=None, input_state=""
        )
        assert tracker.section_frames["body"] == frame_key
        assert tracker.section_frames["footer"] is None
