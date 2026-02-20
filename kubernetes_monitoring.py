#!/usr/bin/env python3

import contextlib
import csv
import datetime
import heapq
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    TypedDict,
    TypeVar,
    cast,
)

from rich import box
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

try:
    API_REQUEST_TIMEOUT = float(os.getenv("KMP_API_TIMEOUT", "10.0"))
except ValueError:
    API_REQUEST_TIMEOUT = 10.0

try:
    _KUBECTL_CACHE_TTL = float(os.getenv("KMP_KUBECTL_CACHE_TTL", "1.5"))
except ValueError:
    _KUBECTL_CACHE_TTL = 0.0

console = Console()

TERMINAL_RESIZED = False
UTC_MIN = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def handle_winch(signum: int, frame: Any) -> None:
    """Signal handler for SIGWINCH to flag terminal resize."""
    global TERMINAL_RESIZED
    TERMINAL_RESIZED = True


# 노드그룹 라벨을 변수로 분리 (기본값: node.kubernetes.io/app)
NODE_GROUP_LABEL = "node.kubernetes.io/app"

SNAPSHOT_EXPORT_DIR = Path("/var/tmp/kmp")
SNAPSHOT_SAVE_COMMANDS = {"s", ":s", "save", ":save", ":export"}
CSV_SAVE_COMMANDS = {"csv", ":csv"}

WINDOWS_INPUT_BUFFER: List[str] = []
POSIX_INPUT_BUFFER: List[str] = []
CURRENT_INPUT_DISPLAY = ""

LIVE_REFRESH_INTERVAL = 2.0  # seconds
INPUT_POLL_INTERVAL = 0.05  # seconds
COMMAND_INPUT_VISIBLE = False

_NODE_SELECTOR_CACHE: Dict[str, Tuple[float, FrozenSet[str]]] = {}
_KUBECTL_JSON_CACHE: Dict[Tuple[str, ...], Tuple[float, Any, Optional[str]]] = {}
_NODE_SELECTOR_CACHE_TTL = float(os.getenv("KMP_NODE_CACHE_TTL", "10.0"))

FrameKey = Tuple[str, Tuple[str, ...]]
_T = TypeVar("_T")

if TYPE_CHECKING:

    class _MsvcrtModule(Protocol):
        def kbhit(self) -> bool: ...

        def getwch(self) -> str: ...


def _normalize_attr_key(key: Any) -> str:
    """Attribute-style 접근을 위한 키 정규화."""
    if isinstance(key, str):
        text = key
    else:
        text = str(key)
    return "".join(ch for ch in text if ch.isalnum()).lower()


class AttrDict:
    """dict를 속성 접근 방식으로 다룰 수 있게 감싸는 래퍼."""

    __slots__ = ("_data", "_key_map")

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data
        key_map: Dict[str, str] = {}
        for original_key in data:
            normalized = _normalize_attr_key(original_key)
            key_map.setdefault(normalized, original_key)
        self._key_map = key_map

    def __getattr__(self, item: str) -> Any:
        actual_key = self._key_map.get(_normalize_attr_key(item))
        if actual_key is None or actual_key not in self._data:
            return None
        value = self._data[actual_key]
        wrapped = _wrap_kubectl_value(value)
        if wrapped is not value:
            self._data[actual_key] = wrapped
        return wrapped

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            actual_key: Any = self._key_map.get(_normalize_attr_key(key), key)
        else:
            actual_key = key
        if actual_key not in self._data:
            return None
        value = self._data[actual_key]
        wrapped = _wrap_kubectl_value(value)
        if wrapped is not value:
            self._data[actual_key] = wrapped
        return wrapped

    def get(self, key: str, default: Any = None) -> Any:
        actual_key = self._key_map.get(_normalize_attr_key(key), key)
        if actual_key not in self._data:
            return default
        value = self._data[actual_key]
        wrapped = _wrap_kubectl_value(value)
        if wrapped is not value:
            self._data[actual_key] = wrapped
        return wrapped

    def to_dict(self) -> Dict[str, Any]:
        return self._data


def _wrap_kubectl_value(value: Any) -> Any:
    if isinstance(value, dict):
        return AttrDict(value)
    if isinstance(value, list):
        return [_wrap_kubectl_value(item) for item in value]
    return value


def _run_kubectl_json(
    args: Sequence[str], *, timeout: float = API_REQUEST_TIMEOUT, cacheable: bool = True
) -> Tuple[Optional[Any], Optional[str], str]:
    """kubectl 명령을 JSON 출력으로 실행한 뒤 결과를 AttrDict로 감싼다."""
    command = ["kubectl", *args, "-o", "json"]
    command_str = shlex.join(command)
    cache_key = tuple(command)
    cached_payload: Optional[Any] = None
    cached_error: Optional[str] = None
    if cacheable and _KUBECTL_CACHE_TTL > 0:
        cached_entry = _KUBECTL_JSON_CACHE.get(cache_key)
        if cached_entry:
            cached_time, payload_obj, error_text = cached_entry
            if time.monotonic() - cached_time <= _KUBECTL_CACHE_TTL:
                return payload_obj, error_text, command_str
            cached_payload = payload_obj
            cached_error = error_text
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if cacheable and cached_payload is not None:
            return cached_payload, cached_error, command_str
        return None, "kubectl command timed out.", command_str
    if completed.returncode != 0:
        error = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "kubectl command failed."
        )
        if cacheable and cached_payload is not None:
            _KUBECTL_JSON_CACHE[cache_key] = (time.monotonic(), cached_payload, error)
            return cached_payload, error, command_str
        return None, error, command_str
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        if cacheable and cached_payload is not None:
            _KUBECTL_JSON_CACHE[cache_key] = (
                time.monotonic(),
                cached_payload,
                str(exc),
            )
            return cached_payload, f"kubectl JSON decode error: {exc}", command_str
        return None, f"kubectl JSON decode error: {exc}", command_str
    wrapped = _wrap_kubectl_value(payload)
    if cacheable and _KUBECTL_CACHE_TTL > 0:
        _KUBECTL_JSON_CACHE[cache_key] = (time.monotonic(), wrapped, None)
    return wrapped, None, command_str


def _set_input_display(text: str) -> None:
    global CURRENT_INPUT_DISPLAY, COMMAND_INPUT_VISIBLE
    CURRENT_INPUT_DISPLAY = text
    COMMAND_INPUT_VISIBLE = bool(text) and text.startswith(":")


def _clear_input_display() -> None:
    _set_input_display("")


def _make_frame_key(tag: str, *parts: str) -> FrameKey:
    return (tag, tuple(parts))


def _select_tail_sorted(
    items: Sequence[_T],
    limit: int,
    *,
    key: Callable[[_T], Any],
) -> List[_T]:
    """큰 key 순으로 상위 limit개를 오름차순으로 반환."""
    if limit <= 0:
        return []
    if limit >= len(items):
        return sorted(items, key=key)
    largest = heapq.nlargest(limit, items, key=key)
    return sorted(largest, key=key)


class PodUsageRecord(TypedDict):
    namespace: str
    pod: str
    cpu_raw: str
    cpu_millicores: int
    memory_raw: str
    memory_bytes: int
    node: str


class SnapshotSaveError(RuntimeError):
    """스냅샷 저장 실패를 구분하기 위한 예외."""

    def __init__(self, path: Path, original: OSError) -> None:
        super().__init__(f"{path}: {original.strerror or original}")
        self.path = path
        self.original = original


@dataclass
class SnapshotPayload:
    """Slack에 저장할 렌더링 스냅샷."""

    title: str
    status: str
    body: str
    command: Optional[str]


@dataclass(frozen=True)
class PodContainerSummary:
    """Pod 컨테이너 Ready/Restart 지표 요약."""

    ready: int
    total: int
    restarts: int
    last_restart_at: Optional[datetime.datetime] = None


@dataclass(frozen=True)
class PodMonitorPriority:
    """Pod Monitoring (생성 순) 테이블 정렬용 메타데이터."""

    pod: AttrDict
    summary: PodContainerSummary
    created_at: Optional[datetime.datetime]
    last_restart_at: Optional[datetime.datetime]
    namespace: str
    pod_name: str

    @property
    def restart_flag(self) -> int:
        return 1 if self.summary.restarts > 0 else 0

    @property
    def activity_timestamp(self) -> datetime.datetime:
        """재시작 시각이 있으면 우선시하고, 없으면 생성 시각 기반으로 정렬."""
        created = self.created_at or UTC_MIN
        last_restart = self.last_restart_at or created
        return last_restart


@dataclass(frozen=True)
class NodeGroupInfo:
    """지정된 라벨 키의 값과 해당 노드 목록."""

    key: str
    value: str
    nodes: Tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.key}={self.value}"

    @property
    def node_count(self) -> int:
        return len(self.nodes)


def _summarize_pod_containers(pod: AttrDict) -> PodContainerSummary:
    """kubectl JSON Pod 객체에서 Ready/총 컨테이너/재시작 횟수를 계산."""
    status = getattr(pod, "status", None)
    spec = getattr(pod, "spec", None)
    container_statuses = list(getattr(status, "container_statuses", None) or [])
    ready = sum(1 for item in container_statuses if getattr(item, "ready", False))
    total = (
        len(container_statuses)
        if container_statuses
        else len(getattr(spec, "containers", []) or [])
    )
    restarts = 0
    latest_restart: Optional[datetime.datetime] = None

    for item in container_statuses:
        restarts += int(getattr(item, "restart_count", 0))

        last_state = getattr(item, "last_state", None)
        terminated = getattr(last_state, "terminated", None)
        finished_at = _ensure_datetime(getattr(terminated, "finished_at", None))
        if finished_at and (latest_restart is None or finished_at > latest_restart):
            latest_restart = finished_at

        state = getattr(item, "state", None)
        current_terminated = getattr(state, "terminated", None)
        current_finished = _ensure_datetime(
            getattr(current_terminated, "finished_at", None)
        )
        if current_finished and (
            latest_restart is None or current_finished > latest_restart
        ):
            latest_restart = current_finished

    return PodContainerSummary(
        ready=ready,
        total=total,
        restarts=restarts,
        last_restart_at=latest_restart,
    )


def _prioritize_pods_for_creation_monitor(
    pods: Sequence[AttrDict],
) -> List[PodMonitorPriority]:
    """재시작된 Pod를 우선적으로 노출하기 위한 정렬 메타데이터를 생성."""
    priorities: List[PodMonitorPriority] = []
    for pod in pods:
        metadata = getattr(pod, "metadata", None)
        created_at = _ensure_datetime(getattr(metadata, "creation_timestamp", None))
        summary = _summarize_pod_containers(pod)
        namespace = getattr(metadata, "namespace", "") or ""
        pod_name = getattr(metadata, "name", "") or ""
        priorities.append(
            PodMonitorPriority(
                pod=pod,
                summary=summary,
                created_at=created_at,
                last_restart_at=summary.last_restart_at,
                namespace=namespace,
                pod_name=pod_name,
            )
        )
    priorities.sort(
        key=lambda entry: (
            entry.activity_timestamp,
            entry.summary.restarts,
            entry.created_at or UTC_MIN,
            entry.namespace,
            entry.pod_name,
        ),
        reverse=True,
    )
    return priorities


@dataclass(frozen=True)
class NodeLabelKeyInfo:
    """노드 라벨 키 개요."""

    key: str
    node_count: int
    sample_values: Tuple[str, ...]


@dataclass(frozen=True)
class NodeLabelSelection:
    """선택된 노드 라벨 key 및 선택적 value."""

    key: str
    value: Optional[str] = None

    @property
    def expression(self) -> Optional[str]:
        if self.value in (None, ""):
            return None
        return f"{self.key}={self.value}"

    def __str__(self) -> str:
        return self.expression or self.key


def _normalize_labels_mapping(labels: Any) -> Dict[str, Any]:
    if isinstance(labels, AttrDict):
        return labels.to_dict()
    if isinstance(labels, dict):
        return labels
    return {}


def _label_value_from_mapping(labels: Any, key: str) -> Optional[str]:
    normalized = _normalize_labels_mapping(labels)
    if not normalized:
        return None
    value = normalized.get(key)
    if value in (None, ""):
        return None
    return str(value)


def _extract_node_label_infos(
    nodes: Sequence[AttrDict], label_key: str
) -> List[NodeGroupInfo]:
    """노드 목록에서 지정된 라벨 키에 대한 값 목록을 정리해 반환."""
    node_groups: Dict[str, Set[str]] = {}
    for node in nodes:
        metadata = getattr(node, "metadata", None)
        labels = getattr(metadata, "labels", None)
        value = _label_value_from_mapping(labels, label_key)
        if not value:
            continue
        node_name = getattr(metadata, "name", "") or ""
        if not node_name:
            continue
        group_nodes = node_groups.setdefault(value, set())
        group_nodes.add(node_name)
    infos = [
        NodeGroupInfo(key=label_key, value=value, nodes=tuple(sorted(node_names)))
        for value, node_names in node_groups.items()
    ]
    return sorted(infos, key=lambda info: info.value.lower())


def _collect_node_label_key_infos(nodes: Sequence[AttrDict]) -> List[NodeLabelKeyInfo]:
    """노드에서 발견된 라벨 키 목록을 요약한다."""
    values_by_key: Dict[str, Set[str]] = {}
    counts_by_key: Dict[str, int] = {}
    for node in nodes:
        metadata = getattr(node, "metadata", None)
        labels = getattr(metadata, "labels", None)
        normalized = _normalize_labels_mapping(labels)
        if not normalized:
            continue
        for raw_key, value in normalized.items():
            if value in (None, ""):
                continue
            key_str = raw_key if isinstance(raw_key, str) else str(raw_key)
            counts_by_key[key_str] = counts_by_key.get(key_str, 0) + 1
            bucket = values_by_key.setdefault(key_str, set())
            bucket.add(str(value))
    infos = [
        NodeLabelKeyInfo(
            key=key,
            node_count=counts_by_key.get(key, 0),
            sample_values=tuple(sorted(values)[:3]),
        )
        for key, values in values_by_key.items()
    ]
    return sorted(infos, key=lambda info: info.key.lower())


def _ensure_datetime(value: Optional[Any]) -> Optional[datetime.datetime]:
    """datetime 또는 ISO 문자열 입력을 UTC datetime으로 정규화."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        candidate = value
    else:
        try:
            text = str(value).replace("Z", "+00:00")
            candidate = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=datetime.timezone.utc)
    return candidate.astimezone(datetime.timezone.utc)


_DISPLAY_TIME_DELTA = datetime.timedelta(hours=9)
_DISPLAY_TIMEZONE = datetime.timezone(_DISPLAY_TIME_DELTA, name="KST")
_TIMEZONE_LABEL = "KST"
_TIME_AWARE_HEADERS = {
    "LastSeen",
    "CreatedAt",
    "LastTerminatedTime",
    "LastRestartAt",
}


def _format_timestamp(value: Optional[datetime.datetime]) -> str:
    """표시용 타임스탬프 문자열(KST) 생성."""
    normalized = _ensure_datetime(value)
    if normalized is None:
        return "-"
    localized = normalized.astimezone(_DISPLAY_TIMEZONE)
    return localized.strftime("%Y-%m-%d %H:%M:%S")


def _label_time_header(header: str) -> str:
    """시간 관련 컬럼에 타임존 정보를 부착한다."""
    if header in _TIME_AWARE_HEADERS:
        return f"{header} ({_TIMEZONE_LABEL})"
    return header


def _label_time_headers(headers: Sequence[str]) -> List[str]:
    """시간 컬럼이 포함된 헤더 목록에 타임존 레이블을 일괄 적용한다."""
    return [_label_time_header(name) for name in headers]


def _parse_tail_count(raw: str) -> int:
    """tail -n 입력 문자열을 안전한 정수로 변환."""
    try:
        count = int(raw)
    except (TypeError, ValueError):
        return 20
    return max(count, 1)


def _sanitize_multiline(text: Optional[str]) -> str:
    """다중 공백을 공백 하나로 축소하고 줄바꿈을 공백으로 치환."""
    if not text:
        return "-"
    return " ".join(str(text).split())


def _event_timestamp(event: Any) -> Optional[datetime.datetime]:
    """CoreV1Event 유사 객체에서 관측 시각을 추출."""
    candidates = [
        getattr(event, "last_timestamp", None),
        getattr(event, "event_time", None),
        getattr(event, "deprecated_last_timestamp", None),
        getattr(getattr(event, "metadata", None), "creation_timestamp", None),
        getattr(event, "first_timestamp", None),
    ]
    for candidate in candidates:
        normalized = _ensure_datetime(candidate)
        if normalized is not None:
            return normalized
    return None


def _node_ready_condition(node: Any) -> str:
    """노드 Ready 조건을 요약 문자열로 반환."""
    conditions = getattr(getattr(node, "status", None), "conditions", None) or []
    for condition in conditions:
        if getattr(condition, "type", "") == "Ready":
            status = getattr(condition, "status", "")
            if status == "True":
                return "Ready"
            if status == "False":
                return "NotReady"
            return status or "-"
    return "-"


def _node_roles(node: Any) -> str:
    """노드 라벨에서 역할(role) 정보를 추출."""
    labels = _normalize_labels_mapping(
        getattr(getattr(node, "metadata", None), "labels", None)
    )
    roles: List[str] = []
    for raw_key in labels.keys():
        if not isinstance(raw_key, str):
            continue
        if not raw_key.startswith("node-role.kubernetes.io/"):
            continue
        suffix = raw_key[len("node-role.kubernetes.io/") :].strip()
        if not suffix:
            suffix = "-"
        roles.append(suffix.rstrip("/"))
    if not roles:
        return "<none>"
    return ",".join(sorted(roles))


def _node_zone(node: Any) -> str:
    """노드에 설정된 가용 영역 라벨을 반환."""
    labels = _normalize_labels_mapping(
        getattr(getattr(node, "metadata", None), "labels", None)
    )
    if not labels:
        return "-"

    zone_value = labels.get("topology.kubernetes.io/zone")
    if zone_value is None:
        zone_value = labels.get("failure-domain.beta.kubernetes.io/zone")

    if zone_value is None:
        return "-"
    return str(zone_value)


def _status_icon(status: str) -> Optional[str]:
    mapping = {
        "success": ":white_check_mark:",
        "error": ":x:",
        "warning": ":warning:",
        "info": ":information_source:",
        "empty": ":information_source:",
    }
    return mapping.get(status, None)


def _style_by_status(value: str, context: str = "pod") -> str:
    """Return a Rich style string based on status value and context."""
    v = value.strip().lower() if value else ""
    if context == "pod":
        if v in ("running", "succeeded"):
            return "green"
        if v in ("pending", "containercreating"):
            return "yellow"
        if v in (
            "failed",
            "crashloopbackoff",
            "imagepullbackoff",
            "errimagepull",
            "error",
            "oomkilled",
            "terminated",
        ):
            return "red"
    elif context == "node":
        if v == "ready":
            return "green"
        if v == "notready":
            return "red"
    elif context == "event":
        if v == "warning":
            return "yellow"
        if v == "normal":
            return "dim"
    return ""


def _row_style_for_status(phase: str, context: str = "pod") -> str:
    """Return a row-level style based on resource status."""
    return _style_by_status(phase, context)


def _render_empty_state(
    tracker: "LiveFrameTracker",
    command: str,
    title: str,
    message: str,
    suggestion: str = "",
) -> None:
    """Render an empty-state panel with optional suggestion text."""
    body = message
    if suggestion:
        body = f"{message}\n[dim]{suggestion}[/dim]"
    frame_key = _make_frame_key("empty")
    snapshot = _format_plain_snapshot(
        SnapshotPayload(
            title=f"{title} - Empty",
            status="empty",
            body=message,
            command=command,
        )
    )
    tracker.update(
        frame_key,
        _compose_group(
            command,
            Panel(
                Text.from_markup(body),
                title="Info",
                style="dim",
                border_style="blue",
            ),
        ),
        snapshot,
        input_state=CURRENT_INPUT_DISPLAY,
    )


def _format_plain_snapshot(payload: SnapshotPayload) -> str:
    """텍스트 기반 스냅샷을 Slack Markdown으로 변환."""
    lines: List[str] = []
    icon = _status_icon(payload.status)
    title = payload.title.strip()
    if icon:
        lines.append(f"*{icon} {title}*")
    else:
        lines.append(f"*{title}*")

    content = payload.body.strip()
    if content:
        lines.extend(["```", content, "```"])
    else:
        lines.extend(["```", "No data available.", "```"])

    return "\n".join(lines)


def _format_table_snapshot(
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    command: str,
    status: str = "info",
) -> str:
    """테이블 데이터를 현재 화면과 유사한 텍스트 코드블록으로 변환.

    `command` 인자는 호환성을 위해 유지하며 렌더링에는 사용하지 않는다.
    """
    sanitized_headers = [str(header).strip() for header in headers]
    column_count = len(sanitized_headers)

    sanitized_rows: List[List[str]] = []
    for row in rows:
        normalized = [
            str(cell).replace("\n", " ").strip() for cell in row[:column_count]
        ]
        if len(normalized) < column_count:
            normalized.extend([""] * (column_count - len(normalized)))
        sanitized_rows.append(normalized)

    table_lines: List[str] = []

    if column_count == 0:
        table_lines.append("(no columns)")
    else:
        column_widths: List[int] = []
        for idx in range(column_count):
            max_width = len(sanitized_headers[idx])
            for row in sanitized_rows:
                if idx < len(row):
                    max_width = max(max_width, len(row[idx]))
            column_widths.append(max_width)

        def _format_row(cells: Sequence[str]) -> str:
            padded: List[str] = []
            for idx in range(column_count):
                cell = cells[idx] if idx < len(cells) else ""
                width = column_widths[idx]
                padded.append(cell.ljust(width))
            return "  ".join(padded).rstrip()

        header_line = _format_row(sanitized_headers)
        divider = "  ".join(
            ("-" * width) if width > 0 else "-" for width in column_widths
        ).rstrip()

        table_lines.append(header_line)
        table_lines.append(divider)
        if sanitized_rows:
            table_lines.extend(_format_row(row) for row in sanitized_rows)
        else:
            table_lines.append("(no rows)")

    icon = _status_icon(status)
    title_text = title.strip() or "Snapshot"
    if icon:
        title_line = f"*{icon} {title_text}*"
    else:
        title_line = f"*{title_text}*"

    snapshot_lines: List[str] = [title_line, "", "```", *table_lines, "```"]
    return "\n".join(snapshot_lines)


def _generate_snapshot_timestamp() -> str:
    """스냅샷 파일명에 사용할 타임스탬프 문자열 생성."""
    return datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def _save_markdown_snapshot(markdown: str, timestamp: Optional[str] = None) -> Path:
    """코드블록 스냅샷 파일을 저장하고 경로 반환."""
    try:
        SNAPSHOT_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotSaveError(SNAPSHOT_EXPORT_DIR, exc) from exc
    snapshot_timestamp = timestamp or _generate_snapshot_timestamp()
    file_path = SNAPSHOT_EXPORT_DIR / f"{snapshot_timestamp}.md"
    try:
        file_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        raise SnapshotSaveError(file_path, exc) from exc
    return file_path


def _save_csv_snapshot(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    timestamp: Optional[str] = None,
) -> Path:
    """CSV 파일을 저장하고 경로 반환."""
    try:
        SNAPSHOT_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotSaveError(SNAPSHOT_EXPORT_DIR, exc) from exc
    snapshot_timestamp = timestamp or _generate_snapshot_timestamp()
    file_path = SNAPSHOT_EXPORT_DIR / f"{snapshot_timestamp}.csv"
    try:
        with file_path.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(headers)
            writer.writerows(rows)
    except OSError as exc:
        raise SnapshotSaveError(file_path, exc) from exc
    return file_path


def _format_ready_ratio(ready: int, total: int) -> str:
    """컨테이너 Ready 비율을 Excel-safe한 문자열로 변환."""
    if total <= 0:
        return "[0/0]"
    normalized_ready = max(0, min(ready, total))
    return f"[{normalized_ready}/{total}]"


def _read_nonblocking_command() -> Optional[str]:
    """사용자 입력(line 단위)을 블로킹 없이 읽어 Slack 저장 요청 감지."""
    if not sys.stdin.isatty():
        return None

    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            return None
        typed_msvcrt = cast("_MsvcrtModule", msvcrt)

        command_ready = None
        while typed_msvcrt.kbhit():
            char = typed_msvcrt.getwch()
            if char in ("\r", "\n"):
                command_ready = "".join(WINDOWS_INPUT_BUFFER).strip()
                WINDOWS_INPUT_BUFFER.clear()
                _clear_input_display()
                # Enter 입력 시 CR/LF 잔여 문자를 비움
                continue
            if char == "\x1b":
                WINDOWS_INPUT_BUFFER.clear()
                _clear_input_display()
                continue
            if char in ("\b", "\x7f"):
                if WINDOWS_INPUT_BUFFER:
                    WINDOWS_INPUT_BUFFER.pop()
                _set_input_display("".join(WINDOWS_INPUT_BUFFER))
                continue
            WINDOWS_INPUT_BUFFER.append(char)
            _set_input_display("".join(WINDOWS_INPUT_BUFFER))
        if command_ready:
            return command_ready
        return None

    # POSIX 계열: select로 입력 여부 확인 후 readline
    import os as posix_os  # type: ignore
    import select  # type: ignore

    try:
        fd = sys.stdin.fileno()
    except OSError:
        return None

    posix_command_ready: Optional[str] = None
    while True:
        readable, _, _ = select.select([fd], [], [], 0)
        if not readable:
            break
        try:
            data = posix_os.read(fd, 1)
        except OSError:
            break
        if not data:
            break
        char = data.decode(errors="ignore")
        if char in ("\n", "\r"):
            posix_command_ready = "".join(POSIX_INPUT_BUFFER).strip()
            POSIX_INPUT_BUFFER.clear()
            _clear_input_display()
            break
        if char == "\x1b":
            POSIX_INPUT_BUFFER.clear()
            _clear_input_display()
            while True:
                readable, _, _ = select.select([fd], [], [], 0)
                if not readable:
                    break
                try:
                    leftover = posix_os.read(fd, 1)
                except OSError:
                    break
                if not leftover:
                    break
            continue
        if char in ("\x7f", "\b"):
            if POSIX_INPUT_BUFFER:
                POSIX_INPUT_BUFFER.pop()
            _set_input_display("".join(POSIX_INPUT_BUFFER))
            continue
        POSIX_INPUT_BUFFER.append(char)
        _set_input_display("".join(POSIX_INPUT_BUFFER))
    if posix_command_ready:
        return posix_command_ready
    return None


def _handle_csv_save(live: Live, tracker: "LiveFrameTracker", command: str) -> None:
    """CSV 저장 요청을 처리."""
    if not tracker.latest_structured_data:
        live.console.print(
            f"\nCommand '{command}' failed: No table data available for CSV export.",
            style="bold yellow",
        )
        return

    try:
        headers = tracker.latest_structured_data["headers"]
        rows = tracker.latest_structured_data["rows"]
        path = _save_csv_snapshot(headers, rows)
    except (SnapshotSaveError, KeyError) as exc:
        live.console.print(
            f"\nCommand '{command}' failed: {exc}",
            style="bold red",
        )
        _clear_input_display()
        return

    live.console.print(
        f"\nCommand '{command}' success: CSV snapshot saved → {path}",
        style="bold green",
    )
    _clear_input_display()


def _handle_snapshot_command(
    live: Live, tracker: "LiveFrameTracker", command: Optional[str]
) -> None:
    """저장 요청이 있는 경우 Markdown 또는 CSV를 파일로 기록."""
    if command is None:
        return

    display_command = command.strip() or "<empty>"
    normalized = command.lower()

    if normalized in CSV_SAVE_COMMANDS:
        _handle_csv_save(live, tracker, display_command)
        return

    if normalized in SNAPSHOT_SAVE_COMMANDS:
        if not tracker.latest_snapshot:
            live.console.print(
                f"\nCommand '{display_command}' failed: No data to save.",
                style="bold yellow",
            )
            return
        markdown_path: Optional[Path] = None
        csv_path: Optional[Path] = None
        try:
            timestamp = _generate_snapshot_timestamp()
            markdown_path = _save_markdown_snapshot(
                tracker.latest_snapshot,
                timestamp=timestamp,
            )
            structured_data = tracker.latest_structured_data
            if (
                structured_data
                and "headers" in structured_data
                and "rows" in structured_data
            ):
                csv_path = _save_csv_snapshot(
                    structured_data["headers"],
                    structured_data["rows"],
                    timestamp=timestamp,
                )
        except SnapshotSaveError as exc:
            with contextlib.suppress(OSError):
                if markdown_path and markdown_path.exists():
                    markdown_path.unlink()
            live.console.print(
                f"\nCommand '{display_command}' failed: {exc}",
                style="bold red",
            )
            _clear_input_display()
            return
        live.console.print(
            "\nCommand '{command}' success: Snapshot saved → {markdown}{csv}".format(
                command=display_command,
                markdown=markdown_path,
                csv=(
                    f" (CSV: {csv_path})" if csv_path is not None else " (no CSV data)"
                ),
            ),
            style="bold green",
        )
        _clear_input_display()
        return

    live.console.print(
        f"\nCommand '{display_command}' is not supported. "
        "Available commands: s, save, csv, ...",
        style="bold yellow",
    )


def _tick_iteration(live: Live, tracker: "LiveFrameTracker") -> None:
    """루프 종료 전 저장 요청을 처리."""
    user_command = _read_nonblocking_command()
    if user_command:
        _handle_snapshot_command(live, tracker, user_command)


class LiveFrameTracker:
    """Live 갱신 및 스냅샷 생성을 추적하는 도우미."""

    def __init__(self, live: Live, view_name: str = "") -> None:
        self.live = live
        self.view_name = view_name
        self._loading = False
        self.layout = Layout(name="root")
        self.layout.split(
            Layout(name="header", size=1),
            Layout(name="input", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3),
            Layout(name="helpbar", size=1),
        )
        # Header bar: breadcrumb + context + loading
        self._update_header()
        self.layout["input"].visible = False
        self.layout["input"].update(Text(""))
        self.layout["body"].update(Text(""))
        self.layout["footer"].visible = False
        self.layout["footer"].update(Text(""))
        # Help bar: persistent footer
        help_text = Text.from_markup(
            "[dim]:s[/dim] save  [dim]:csv[/dim] export CSV  "
            "[dim]Ctrl+C[/dim] back to menu"
        )
        self.layout["helpbar"].update(help_text)
        self.section_frames: Dict[str, Optional[FrameKey]] = {
            "input": None,
            "body": None,
            "footer": None,
        }
        self.latest_snapshot: Optional[str] = None
        self.latest_structured_data: Optional[Dict[str, Any]] = None
        self.last_input_state: str = ""
        self.live.update(self.layout)

    def _update_header(self) -> None:
        """Render the header bar with view name, context, and loading state."""
        ctx = _get_current_context()
        parts = []
        if self.view_name:
            parts.append(f"[bold cyan]{self.view_name}[/bold cyan]")
        parts.append(f"[dim]{ctx}[/dim]")
        parts.append(f"[dim]Auto-refresh: {LIVE_REFRESH_INTERVAL:.0f}s[/dim]")
        if self._loading:
            parts.append("[bold yellow]loading...[/bold yellow]")
        header_text = Text.from_markup("  |  ".join(parts))
        self.layout["header"].update(header_text)

    def show_loading(self) -> None:
        """Show loading indicator in the header bar."""
        if not self._loading:
            self._loading = True
            self._update_header()
            self.live.refresh()

    def clear_loading(self) -> None:
        """Clear loading indicator from the header bar."""
        if self._loading:
            self._loading = False
            self._update_header()
            self.live.refresh()

    def _sync_input_panel(
        self,
        current_input_state: str,
        input_renderable: Optional[RenderableType] = None,
    ) -> bool:
        visible = COMMAND_INPUT_VISIBLE
        visibility_flag = "visible" if visible else "hidden"
        input_key: FrameKey = ("input", (visibility_flag, current_input_state))
        if input_key == self.section_frames["input"]:
            return False
        self.section_frames["input"] = input_key
        self.last_input_state = current_input_state
        self.layout["input"].visible = visible
        if visible:
            self.layout["input"].update(input_renderable or _CommandInputPanel())
        else:
            self.layout["input"].update(Text(""))
        return True

    def update(
        self,
        frame_key: FrameKey,
        renderable: RenderableType,
        snapshot_markdown: Optional[str],
        structured_data: Optional[Dict[str, Any]] = None,
        input_state: Optional[str] = None,
    ) -> None:
        # Auto-clear loading on frame update
        if self._loading:
            self._loading = False
            self._update_header()

        current_input_state = (
            input_state if input_state is not None else CURRENT_INPUT_DISPLAY
        )
        body_renderable: RenderableType = renderable
        footer_renderable: Optional[RenderableType] = None
        command_descriptor = ""
        input_renderable: Optional[RenderableType] = None

        if isinstance(renderable, _FrameRenderable):
            body_renderable = _merge_renderables(renderable.body_renderables)
            command_descriptor = renderable.command
            input_renderable = renderable.input_panel
            footer_renderable = renderable.footer_panel

        changed = self._sync_input_panel(current_input_state, input_renderable)

        if frame_key != self.section_frames["body"]:
            self.section_frames["body"] = frame_key
            self.layout["body"].update(body_renderable)
            changed = True

        previous_footer = self.section_frames["footer"]
        if footer_renderable is None:
            if previous_footer is not None:
                self.section_frames["footer"] = None
                self.layout["footer"].visible = False
                self.layout["footer"].update(Text(""))
                changed = True
        else:
            footer_key: FrameKey = ("footer", (command_descriptor,))
            if footer_key != previous_footer:
                self.section_frames["footer"] = footer_key
                self.layout["footer"].visible = True
                self.layout["footer"].update(footer_renderable)
                changed = True

        if changed:
            self.live.refresh()
        if snapshot_markdown is not None:
            self.latest_snapshot = snapshot_markdown
        if structured_data is not None:
            self.latest_structured_data = structured_data
        else:
            self.latest_structured_data = None

    def tick(self, interval: float = LIVE_REFRESH_INTERVAL) -> None:
        global TERMINAL_RESIZED
        deadline = time.monotonic() + interval
        while True:
            if TERMINAL_RESIZED:
                TERMINAL_RESIZED = False
                self.live.refresh()

            _tick_iteration(self.live, self)
            if self._sync_input_panel(CURRENT_INPUT_DISPLAY):
                self.live.refresh()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(INPUT_POLL_INTERVAL, remaining))


def _parse_cpu_to_millicores(value: str) -> int:
    """CPU 문자열을 milli-core 단위 정수로 변환."""
    cpu = value.strip().lower()
    if not cpu:
        return 0
    try:
        if cpu.endswith("m"):
            return int(float(cpu[:-1]))
        if cpu.endswith("n"):
            return int(float(cpu[:-1]) / 1_000_000)
        return int(float(cpu) * 1000)
    except ValueError:
        return 0


def _parse_memory_to_bytes(value: str) -> int:
    """Memory 문자열을 byte 단위 정수로 변환."""
    mem = value.strip()
    if not mem:
        return 0
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "Ei": 1024**6,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
        "E": 1000**6,
    }
    for suffix, multiplier in units.items():
        if mem.endswith(suffix):
            number = mem[: -len(suffix)]
            try:
                return int(float(number) * multiplier)
            except ValueError:
                return 0
    try:
        return int(float(mem))
    except ValueError:
        return 0


def _run_shell_command(command: str) -> Tuple[str, Optional[str]]:
    """주어진 셸 명령을 실행하고 결과를 반환."""
    completed = subprocess.run(
        ["bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        error_message = completed.stderr.strip() or "command execution failed."
        return "", error_message
    return completed.stdout, None


def _merge_renderables(renderables: Sequence[RenderableType]) -> RenderableType:
    """렌더러 목록을 단일 RenderableType으로 축약."""
    if not renderables:
        return Text("")
    if len(renderables) == 1:
        return renderables[0]
    return Group(*renderables)


class _CommandInputPanel:
    """COMMAND_INPUT_VISIBLE 상태에 따라 패널을 조건부로 출력."""

    def __rich_console__(self, console: Console, options):  # type: ignore[override]
        if not COMMAND_INPUT_VISIBLE:
            return
        display = CURRENT_INPUT_DISPLAY
        prompt = display if display else ":"
        style = "bold cyan" if display else "dim"
        yield Panel(
            Text(prompt, style=style),
            title="command input",
            border_style="cyan",
        )


class _FrameRenderable:
    """입력 패널과 메인 콘텐츠를 하나의 렌더러로 묶는다."""

    def __init__(self, command: str, *renderables: RenderableType) -> None:
        self.command = command
        self.body_renderables: List[RenderableType] = list(renderables)
        self._input_panel = _CommandInputPanel()
        self._footer_panel: Optional[RenderableType] = None

    def __rich_console__(self, console: Console, options):  # type: ignore[override]
        yield self._input_panel
        for item in self.body_renderables:
            yield item
        if self._footer_panel is not None:
            yield self._footer_panel

    @property
    def input_panel(self) -> RenderableType:
        return self._input_panel

    @property
    def footer_panel(self) -> Optional[RenderableType]:
        return self._footer_panel


def _compose_group(command: str, *renderables: RenderableType) -> _FrameRenderable:
    """메인 콘텐츠와 메타데이터(command)를 함께 추적할 렌더러 생성."""
    return _FrameRenderable(command, *renderables)


@contextlib.contextmanager
def suppress_terminal_echo() -> Iterator[None]:
    """Live 화면 동안 터미널 입력 에코를 비활성화."""
    if not sys.stdin.isatty():
        yield
        return

    if os.name == "nt":
        import ctypes
        import msvcrt

        typed_msvcrt = cast("_MsvcrtModule", msvcrt)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_ulong()
        if handle == ctypes.c_void_p(-1).value or not kernel32.GetConsoleMode(
            handle, ctypes.byref(mode)
        ):
            yield
            return
        new_mode = mode.value & ~0x4  # ENABLE_ECHO_INPUT
        if not kernel32.SetConsoleMode(handle, new_mode):
            yield
            return
        try:
            yield
        finally:
            kernel32.SetConsoleMode(handle, mode.value)
            while typed_msvcrt.kbhit():
                typed_msvcrt.getwch()
    else:
        import termios

        fd = sys.stdin.fileno()
        try:
            old_attrs = termios.tcgetattr(fd)
        except termios.error:
            yield
            return
        new_attrs = termios.tcgetattr(fd)
        new_attrs[3] &= ~(termios.ECHO | termios.ICANON)
        new_attrs[6][termios.VMIN] = 0
        new_attrs[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, new_attrs)
        except termios.error:
            yield
            return
        try:
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old_attrs)
            termios.tcflush(fd, termios.TCIFLUSH)


def _get_kubectl_top_pod(
    namespace: Optional[str],
) -> Tuple[List[Tuple[str, str, str, str]], Optional[str], str]:
    """kubectl top pod 호출 결과를 파싱."""
    base_cmd = ["kubectl", "top", "pod", "--no-headers"]
    if namespace:
        cmd = base_cmd + ["-n", namespace]
    else:
        cmd = base_cmd + ["-A"]
    completed = subprocess.run(
        cmd, check=False, capture_output=True, text=True, encoding="utf-8"
    )
    if completed.returncode != 0:
        error_message = completed.stderr.strip() or "kubectl top pod command failed."
        return [], error_message, shlex.join(cmd)
    metrics: List[Tuple[str, str, str, str]] = []
    raw_lines = completed.stdout.strip().splitlines()
    for line in raw_lines:
        parts = line.split()
        if not parts:
            continue
        if namespace:
            if len(parts) < 3:
                continue
            pod_name, cpu, memory = parts[:3]
            metrics.append((namespace, pod_name, cpu, memory))
        else:
            if len(parts) < 4:
                continue
            ns_name, pod_name, cpu, memory = parts[:4]
            metrics.append((ns_name, pod_name, cpu, memory))
    return metrics, None, shlex.join(cmd)


def _map_pod_to_node(namespace: Optional[str] = None) -> Dict[Tuple[str, str], str]:
    """Pod -> Node 매핑을 생성."""
    pods, error, _ = get_pods(namespace)
    if error:
        return {}
    mapping: Dict[Tuple[str, str], str] = {}
    for pod in pods:
        ns_name = getattr(pod.metadata, "namespace", None)
        pod_name = getattr(pod.metadata, "name", None)
        node_name = getattr(getattr(pod, "spec", None), "node_name", None)
        if ns_name and pod_name and node_name:
            mapping[(ns_name, pod_name)] = node_name
    return mapping


def _collect_nodes_for_selector(selection: NodeLabelSelection) -> Set[str]:
    """선택한 라벨 key=value에 속한 노드 이름 집합을 반환."""
    expression = selection.expression
    if not expression:
        return set()
    now = time.monotonic()
    cached = _NODE_SELECTOR_CACHE.get(expression)
    if cached and now - cached[0] <= _NODE_SELECTOR_CACHE_TTL:
        return set(cached[1])

    payload, error, _ = _run_kubectl_json(["get", "nodes", "-l", expression])
    if error or payload is None:
        if cached:
            return set(cached[1])
        return set()
    result: Set[str] = set()
    for node in getattr(payload, "items", []) or []:
        metadata = getattr(node, "metadata", None)
        name = getattr(metadata, "name", None)
        if name:
            result.add(str(name))
    _NODE_SELECTOR_CACHE[expression] = (now, frozenset(result))
    return result


def _node_label_value(node: AttrDict, label_key: str) -> Optional[str]:
    metadata = getattr(node, "metadata", None)
    labels = getattr(metadata, "labels", None)
    return _label_value_from_mapping(labels, label_key)


_CURRENT_CONTEXT_CACHE: Optional[Tuple[float, str]] = None
_CURRENT_CONTEXT_TTL = 30.0


def _get_current_context() -> str:
    """Return the current kubectl context name, cached for performance."""
    global _CURRENT_CONTEXT_CACHE
    now = time.monotonic()
    if (
        _CURRENT_CONTEXT_CACHE
        and now - _CURRENT_CONTEXT_CACHE[0] <= _CURRENT_CONTEXT_TTL
    ):
        return _CURRENT_CONTEXT_CACHE[1]
    try:
        result = subprocess.run(
            ["kubectl", "config", "current-context"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5.0,
        )
        ctx = result.stdout.strip() if result.returncode == 0 else "unknown"
    except (subprocess.TimeoutExpired, OSError):
        ctx = "unknown"
    _CURRENT_CONTEXT_CACHE = (now, ctx)
    return ctx


def cleanup() -> None:
    """리소스 정리 및 종료 전 후처리.

    현재는 외부 리소스를 별도로 잡지 않지만, 추후 확장을 대비해
    공통 정리 지점을 한곳으로 모읍니다.
    """
    # 필요한 경우, 추가 정리 작업을 이곳에 배치합니다.
    console.print("Cleaning up...", style="dim")


def _exit_with_cleanup(code: int, message: str, style: str = "bold yellow") -> None:
    """메시지를 출력하고 정리 후 지정된 코드로 종료."""
    console.print()
    console.print(message, style=style)
    cleanup()
    sys.exit(code)


def setup_asyncio_graceful_shutdown() -> None:
    """
    asyncio 사용 시 SIGINT/SIGTERM를 graceful 하게 처리하기 위한 유틸리티.

    이 함수는 런타임에 호출될 때만 의존성을 import 하며, 현재 스크립트가
    동기 방식으로 동작할 때는 불필요한 오버헤드를 만들지 않습니다.
    """
    try:
        import asyncio
        import contextlib
        import signal
    except ImportError:
        return

    loop = asyncio.get_event_loop()

    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        console.print(
            f"Signal received: {signal.Signals(sig).name}. Initiating graceful shutdown.",
            style="bold yellow",
        )
        stop_event.set()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, _handle_signal, sig)
            except NotImplementedError:
                # Windows 등 일부 환경에서는 add_signal_handler 미지원
                pass

    async def _graceful_shutdown(timeout: float = 10.0) -> None:
        await stop_event.wait()
        tasks = [
            t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task(loop)
        ]
        for t in tasks:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        cleanup()

    # 호출 측에서 생성한 메인 코루틴과 함께 사용하도록 안내 목적.
    # 예: await asyncio.gather(main(), _graceful_shutdown())
    globals()["_async_graceful_shutdown"] = _graceful_shutdown  # for advanced usage


def choose_namespace() -> Optional[str]:
    """
    클러스터의 모든 namespace 목록을 표시하고, 사용자가 index로 선택
    아무 입력도 없으면 전체(namespace 전체) 조회
    """
    payload, error, command = _run_kubectl_json(["get", "namespaces"])
    if error or payload is None:
        console.print(
            "Failed to fetch namespace information.",
            style="bold red",
        )
        detail = error or "kubectl returned an empty response."
        console.print(detail, style="bold yellow")
        console.print(f"Command: {command}", style="dim")
        return None

    items = getattr(payload, "items", []) or []
    if not items:
        console.print("No namespaces found.", style="bold yellow")
        return None

    table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
    table.add_column("Index", style="bold green", width=5)
    table.add_column("Namespace", overflow="fold")
    for idx, ns in enumerate(items, start=1):
        table.add_row(str(idx), ns.metadata.name)
    console.print("\n=== Available Namespaces ===", style="bold green")
    console.print(table)

    selection = Prompt.ask("Select namespace number (default: all)", default="")
    if not selection:
        return None
    if not selection.isdigit():
        console.print(
            "Invalid input. Proceeding with all namespaces.", style="bold yellow"
        )
        return None
    index = int(selection)
    if index < 1 or index > len(items):
        console.print(
            "Invalid number. Proceeding with all namespaces.", style="bold yellow"
        )
        return None
    chosen_ns = str(items[index - 1].metadata.name)
    return chosen_ns


def _label_column_title(label_key: str) -> str:
    return "NodeGroup" if label_key == NODE_GROUP_LABEL else f"Label[{label_key}]"


def choose_node_group() -> Optional[NodeLabelSelection]:
    """
    클러스터 노드에서 사용할 라벨 키와 값을 선택한다.
    선택된 key/value 조합을 NodeLabelSelection으로 반환하며, 값이 없으면 필터 없이 라벨만 표시한다.
    """
    payload, error, command = _run_kubectl_json(["get", "nodes"])
    if error or payload is None:
        console.print(
            "Failed to fetch node information.",
            style="bold red",
        )
        detail = error or "kubectl returned an empty response."
        console.print(detail, style="bold yellow")
        console.print(f"Command: {command}", style="dim")
        return None

    nodes = list(getattr(payload, "items", []) or [])

    key_infos = _collect_node_label_key_infos(nodes)
    if not key_infos:
        console.print("No node labels found.", style="bold yellow")
        return None

    table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
    table.add_column("Index", style="bold green", width=5)
    table.add_column("Label Key", overflow="fold")
    table.add_column("Nodes", justify="right")
    table.add_column("Sample Values", overflow="fold")
    default_key_index = None
    for idx, label_info in enumerate(key_infos, start=1):
        if label_info.key == NODE_GROUP_LABEL and default_key_index is None:
            default_key_index = idx
        sample_values = ", ".join(label_info.sample_values) or "-"
        table.add_row(
            str(idx),
            label_info.key,
            str(label_info.node_count),
            sample_values,
        )
    console.print("\n=== Available Node Label Keys ===", style="bold green")
    console.print(table)

    default_prompt = str(default_key_index) if default_key_index else "1"
    selection = Prompt.ask("Select label key number", default=default_prompt).strip()
    if selection and not selection.isdigit():
        console.print(
            "Please enter a number. Selection cancelled.", style="bold yellow"
        )
        return None
    if selection:
        key_index = int(selection)
    else:
        key_index = int(default_prompt)
    if key_index < 1 or key_index > len(key_infos):
        console.print("Invalid number. Selection cancelled.", style="bold yellow")
        return None
    chosen_key = key_infos[key_index - 1].key

    value_infos = _extract_node_label_infos(nodes, chosen_key)
    if not value_infos:
        console.print(
            f"No values found for label key '{chosen_key}'. Displaying label only.",
            style="bold yellow",
        )
        return NodeLabelSelection(key=chosen_key)

    value_table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
    value_table.add_column("Index", style="bold green", width=5)
    value_table.add_column("Label", overflow="fold")
    value_table.add_column("Nodes", justify="right")
    value_table.add_column("Sample Nodes", overflow="fold")
    for idx, group_info in enumerate(value_infos, start=1):
        sample_nodes = ", ".join(group_info.nodes[:3])
        if len(group_info.nodes) > 3:
            sample_nodes = f"{sample_nodes}, ..."
        if not sample_nodes:
            sample_nodes = "-"
        value_table.add_row(
            str(idx),
            group_info.label,
            str(group_info.node_count),
            sample_nodes,
        )
    console.print("\n=== Available Label Values ===", style="bold green")
    console.print(value_table)

    value_selection = Prompt.ask(
        "Select label value to filter (default: no filter)", default=""
    ).strip()
    if not value_selection:
        console.print(
            f"Displaying label '{chosen_key}' without filter.",
            style="bold green",
        )
        return NodeLabelSelection(key=chosen_key)
    if not value_selection.isdigit():
        console.print(
            "Please enter a number. Proceeding without filter.", style="bold yellow"
        )
        return None
    value_index = int(value_selection)
    if value_index < 1 or value_index > len(value_infos):
        console.print("Invalid number. Proceeding without filter.", style="bold yellow")
        return None
    chosen_info = value_infos[value_index - 1]
    console.print(
        f"Selected label: {chosen_info.label} ({chosen_info.node_count} nodes)",
        style="bold green",
    )
    return NodeLabelSelection(key=chosen_info.key, value=chosen_info.value)


def get_tail_lines(prompt="How many lines? (default: 20): ") -> str:
    """
    tail -n에 사용할 숫자 입력 (기본값 20)
    """
    val = Prompt.ask(prompt, default="20")
    if val.isdigit():
        return val
    else:
        console.print("Please enter a number.", style="bold red")
        return "20"


def get_pods(
    namespace: Optional[str] = None,
) -> Tuple[List[AttrDict], Optional[str], str]:
    """
    지정된 namespace 또는 전체 namespace에서 Pod 목록을 가져옵니다.
    """
    args: List[str] = ["get", "pods"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("-A")
    args.extend(["--chunk-size=0"])
    payload, error, command = _run_kubectl_json(args)
    if error or payload is None:
        return [], error or "kubectl returned an empty response.", command
    items = getattr(payload, "items", []) or []
    return list(items), None, command


def get_nodes(
    label_selector: Optional[str] = None,
) -> Tuple[List[AttrDict], Optional[str], str]:
    """노드 목록을 조회한다 (필터 여부와 관계없이 전체를 가져와 메모리 캐시)."""
    args: List[str] = ["get", "nodes", "--chunk-size=0"]
    payload, error, command = _run_kubectl_json(args)
    if error or payload is None:
        return [], error or "kubectl returned an empty response.", command
    items = list(getattr(payload, "items", []) or [])
    if label_selector:
        key, _, value = label_selector.partition("=")
        if value:
            filtered = [
                node
                for node in items
                if _label_value_from_mapping(
                    getattr(getattr(node, "metadata", None), "labels", None),
                    key,
                )
                == value
            ]
            return filtered, None, command
    return items, None, command


def _handle_kubectl_fetch_error(
    tracker: "LiveFrameTracker",
    *,
    command: str,
    context: str,
    detail: str,
    snapshot_title: str,
) -> None:
    """kubectl 호출 오류를 공통 형식으로 렌더링."""
    normalized = detail or "kubectl returned an empty response."
    is_timeout = "timeout" in normalized.lower()
    status = "warning" if is_timeout else "error"
    panel_style = "bold yellow" if is_timeout else "bold red"
    panel_title = "Warning" if is_timeout else "Error"
    if is_timeout:
        message = (
            f"Delay occurred while {context}. Check network connection and auth status."
        )
    else:
        message = f"Error occurred while {context}."
    body_text = f"{message}\nDetails: {normalized}"
    frame_key = _make_frame_key("kubectl_error", context, normalized)
    snapshot = _format_plain_snapshot(
        SnapshotPayload(
            title=snapshot_title,
            status=status,
            body=body_text,
            command=command,
        )
    )
    tracker.update(
        frame_key,
        _compose_group(
            command,
            Panel(body_text, title=panel_title, style=panel_style),
        ),
        snapshot,
        input_state=CURRENT_INPUT_DISPLAY,
    )


def watch_event_monitoring() -> None:
    """
    1) Event Monitoring
       전체 이벤트 또는 비정상 이벤트(!=Normal)를 확인
    """
    console.print("\n[1] Event Monitoring", style="bold blue")
    ns = choose_namespace()
    event_choice = Prompt.ask(
        "Which events to view? (1: All events (default), 2: Abnormal events (!=Normal))",
        default="1",
    )
    tail_num_raw = get_tail_lines("How many lines to display? (e.g. 20): ")
    tail_limit = _parse_tail_count(tail_num_raw)

    field_selector = "type!=Normal" if event_choice == "2" else None
    kubectl_args: List[str] = ["get", "events"]
    if ns:
        kubectl_args.extend(["-n", ns])
    else:
        kubectl_args.append("-A")
    if field_selector:
        kubectl_args.extend(["--field-selector", field_selector])
    console.print("\n(Press Ctrl+C to stop and return to menu.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, auto_refresh=False) as live:
                tracker = LiveFrameTracker(live, view_name="Event Monitoring")
                while True:
                    tracker.show_loading()
                    payload, error, command_descriptor = _run_kubectl_json(kubectl_args)
                    if error or payload is None:
                        detail = error or "kubectl returned an empty response."
                        _handle_kubectl_fetch_error(
                            tracker,
                            command=command_descriptor,
                            context="fetching event data",
                            detail=detail,
                            snapshot_title="Event Monitoring - Error",
                        )
                        tracker.tick()
                        continue

                    items = list(getattr(payload, "items", []) or [])
                    if not items:
                        _render_empty_state(
                            tracker,
                            command_descriptor,
                            "Event Monitoring",
                            "No events to display.",
                            suggestion="Try broadening the namespace filter or selecting all events.",
                        )
                        tracker.tick()
                        continue

                    selected = _select_tail_sorted(
                        items,
                        tail_limit,
                        key=lambda event: _event_timestamp(event) or UTC_MIN,
                    )
                    table = Table(
                        show_header=True, header_style="bold magenta", box=box.ROUNDED
                    )
                    table.add_column("Namespace", style="bold green", overflow="fold")
                    table.add_column(_label_time_header("LastSeen"))
                    table.add_column("Type")
                    table.add_column("Reason")
                    table.add_column("Object")
                    table.add_column("Message", overflow="fold")

                    markdown_rows: List[List[str]] = []
                    frame_parts: List[str] = []
                    for event in selected:
                        metadata = getattr(event, "metadata", None)
                        namespace = getattr(metadata, "namespace", "-") or "-"
                        last_seen = _format_timestamp(_event_timestamp(event))
                        event_type = getattr(event, "type", "") or "-"
                        reason = getattr(event, "reason", "") or "-"
                        involved = getattr(event, "involved_object", None)
                        involved_name = getattr(involved, "name", "") or "-"
                        involved_kind = getattr(involved, "kind", "")
                        object_ref = (
                            f"{involved_kind}/{involved_name}"
                            if involved_kind
                            else involved_name
                        )
                        message = _sanitize_multiline(getattr(event, "message", None))
                        count = getattr(event, "count", 0) or 0

                        table.add_row(
                            namespace,
                            last_seen,
                            event_type,
                            reason,
                            object_ref,
                            message,
                            style=_row_style_for_status(event_type, "event"),
                        )
                        markdown_rows.append(
                            [
                                namespace,
                                last_seen,
                                event_type,
                                reason,
                                object_ref,
                                message,
                            ]
                        )
                        frame_parts.append(
                            "|".join(
                                [
                                    namespace,
                                    last_seen,
                                    event_type,
                                    reason,
                                    object_ref,
                                    message,
                                    str(count),
                                ]
                            )
                        )

                    frame_key = _make_frame_key("data", *frame_parts)
                    headers = _label_time_headers(
                        [
                            "Namespace",
                            "LastSeen",
                            "Type",
                            "Reason",
                            "Object",
                            "Message",
                        ]
                    )
                    snapshot = _format_table_snapshot(
                        title="Event Monitoring",
                        headers=headers,
                        rows=markdown_rows,
                        command=command_descriptor,
                        status="success",
                    )
                    structured_data = {"headers": headers, "rows": markdown_rows}
                    tracker.update(
                        frame_key,
                        _compose_group(command_descriptor, table),
                        snapshot,
                        structured_data=structured_data,
                        input_state=CURRENT_INPUT_DISPLAY,
                    )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\nReturning to menu...", style="bold yellow")


def view_restarted_container_logs() -> None:
    """
    2) Container Monitoring (재시작된 컨테이너 및 로그)
       최근 재시작된 컨테이너 목록에서 선택하여 이전 컨테이너의 로그 확인
    """
    console.print("\n[2] Container Restart Logs", style="bold blue")
    ns = choose_namespace()
    pods, error, command = get_pods(ns)
    if error:
        console.print(
            "Failed to fetch pod information.",
            style="bold red",
        )
        console.print(f"{error}", style="bold yellow")
        console.print(f"Command: {command}", style="dim")
        return
    if not pods:
        return

    restarted_containers = []
    for pod in pods:
        ns_pod = pod.metadata.namespace
        p_name = pod.metadata.name
        if not pod.status.container_statuses:
            continue
        for c_status in pod.status.container_statuses:
            term = c_status.last_state.terminated
            if term and term.finished_at:
                finished_at = term.finished_at
                if isinstance(finished_at, str):
                    finished_at = datetime.datetime.fromisoformat(
                        finished_at.replace("Z", "+00:00")
                    )
                restarted_containers.append(
                    (ns_pod, p_name, c_status.name, finished_at)
                )
    restarted_containers.sort(key=lambda x: x[3], reverse=True)
    line_count = int(get_tail_lines("How many containers to display? (e.g. 20): "))
    displayed_containers = restarted_containers[:line_count]

    if not displayed_containers:
        console.print("No recently restarted containers found.", style="bold yellow")
        return

    table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
    table.add_column("INDEX", style="bold green", width=5)
    table.add_column("Namespace", overflow="fold")
    table.add_column("Pod", overflow="fold")
    table.add_column("Container", overflow="fold")
    table.add_column(_label_time_header("LastTerminatedTime"))
    for i, (ns_pod, p_name, c_name, fat) in enumerate(displayed_containers, start=1):
        table.add_row(str(i), ns_pod, p_name, c_name, _format_timestamp(fat))
    console.print(
        f"\n=== Recently Restarted Containers (Top {line_count} by time) ===\n",
        style="bold green",
    )
    console.print(table)

    sel = Prompt.ask("\nEnter INDEX to view logs (Q: quit)", default="").strip()
    if sel.upper() == "Q" or not sel.isdigit():
        return
    idx = int(sel)
    if idx < 1 or idx > len(displayed_containers):
        console.print("Index out of range.", style="bold red")
        return
    ns_pod, p_name, c_name, _ = displayed_containers[idx - 1]
    log_tail = Prompt.ask("How many log lines? (default: 50)", default="50").strip()
    if not log_tail.isdigit():
        console.print("Invalid number. Displaying 50 lines.", style="bold red")
        log_tail = "50"
    cmd_args = [
        "kubectl",
        "logs",
        "-n",
        ns_pod,
        "-p",
        p_name,
        "-c",
        c_name,
        f"--tail={log_tail}",
    ]
    console.print(f"\nCommand: {shlex.join(cmd_args)}\n", style="dim")
    subprocess.run(cmd_args, check=False)


def watch_pod_monitoring_by_creation() -> None:
    """
    3) Pod Monitoring (생성된 순서)
       Pod IP 및 Node Name을 선택적으로 표시하며, namespace 지정 가능
    """
    console.print("\n[3] Pod List (by creation)", style="bold blue")
    ns = choose_namespace()
    extra = (
        Prompt.ask("Show Pod IP and Node Name? (yes/no)", default="no").strip().lower()
    )
    show_extra = extra.startswith("y")
    tail_num_raw = get_tail_lines("How many lines to display? (e.g. 20): ")
    tail_limit = _parse_tail_count(tail_num_raw)

    console.print("\n(Press Ctrl+C to stop and return to menu.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, auto_refresh=False) as live:
                tracker = LiveFrameTracker(live, view_name="Pod List (by creation)")
                while True:
                    tracker.show_loading()
                    pods, error, command_descriptor = get_pods(ns)
                    if error:
                        _handle_kubectl_fetch_error(
                            tracker,
                            command=command_descriptor,
                            context="fetching pod data",
                            detail=error,
                            snapshot_title="Pod List (by creation) - Error",
                        )
                        tracker.tick()
                        continue

                    if not pods:
                        _render_empty_state(
                            tracker,
                            command_descriptor,
                            "Pod List (by creation)",
                            "No results to display.",
                            suggestion="Try selecting a different namespace.",
                        )
                        tracker.tick()
                        continue

                    prioritized = _prioritize_pods_for_creation_monitor(pods)
                    selected = prioritized[:tail_limit]

                    table = Table(
                        show_header=True, header_style="bold magenta", box=box.ROUNDED
                    )
                    include_namespace = ns is None
                    if include_namespace:
                        table.add_column(
                            "Namespace", style="bold green", overflow="fold"
                        )
                    table.add_column("Name", overflow="fold")
                    table.add_column("Ready")
                    table.add_column("Status")
                    table.add_column("Restarts", justify="right")
                    table.add_column(_label_time_header("LastRestartAt"))
                    table.add_column(_label_time_header("CreatedAt"))
                    if show_extra:
                        table.add_column("PodIP")
                        table.add_column("Node", overflow="fold")

                    markdown_rows: List[List[str]] = []
                    frame_parts: List[str] = []
                    for entry in selected:
                        pod = entry.pod
                        status = getattr(pod, "status", None)
                        spec = getattr(pod, "spec", None)

                        namespace = entry.namespace or "-"
                        name = entry.pod_name or "-"
                        creation = _format_timestamp(entry.created_at)
                        summary = entry.summary
                        last_restart = _format_timestamp(entry.last_restart_at)
                        ready_display = _format_ready_ratio(
                            summary.ready,
                            summary.total,
                        )
                        restarts = summary.restarts
                        phase = getattr(status, "phase", "") or "-"
                        pod_ip = getattr(status, "pod_ip", "") or "-"
                        node_name = getattr(spec, "node_name", "") or "-"

                        row: List[str] = []
                        if include_namespace:
                            row.append(namespace)
                        row.extend(
                            [
                                name,
                                ready_display,
                                phase,
                                str(restarts),
                                last_restart,
                                creation,
                            ]
                        )
                        if show_extra:
                            row.extend([pod_ip, node_name])
                        table.add_row(*row, style=_row_style_for_status(phase, "pod"))

                        markdown_row = row.copy()
                        markdown_rows.append(markdown_row)
                        frame_parts.append(
                            "|".join(
                                row
                                + (
                                    [
                                        namespace if include_namespace else "",
                                        pod_ip if show_extra else "",
                                        node_name if show_extra else "",
                                    ]
                                )
                            )
                        )

                    frame_key = _make_frame_key("data", *frame_parts)
                    headers = (
                        [
                            "Namespace",
                            "Name",
                            "Ready",
                            "Status",
                            "Restarts",
                            "LastRestartAt",
                            "CreatedAt",
                        ]
                        if include_namespace
                        else [
                            "Name",
                            "Ready",
                            "Status",
                            "Restarts",
                            "LastRestartAt",
                            "CreatedAt",
                        ]
                    )
                    if show_extra:
                        headers.extend(["PodIP", "Node"])
                    headers = _label_time_headers(headers)
                    snapshot = _format_table_snapshot(
                        title="Pod List (by creation)",
                        headers=headers,
                        rows=markdown_rows,
                        command=command_descriptor,
                        status="success",
                    )
                    structured_data = {"headers": headers, "rows": markdown_rows}
                    tracker.update(
                        frame_key,
                        _compose_group(command_descriptor, table),
                        snapshot,
                        structured_data=structured_data,
                        input_state=CURRENT_INPUT_DISPLAY,
                    )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\nReturning to menu...", style="bold yellow")


def watch_non_running_pod() -> None:
    """
    4) Pod Monitoring (Running이 아닌 Pod)
       Pod IP 및 Node Name을 선택적으로 표시하며, namespace 지정 가능
    """
    console.print("\n[4] Non-Running Pods", style="bold blue")
    ns = choose_namespace()
    extra = (
        Prompt.ask("Show Pod IP and Node Name? (yes/no)", default="no").strip().lower()
    )
    show_extra = extra.startswith("y")
    tail_num_raw = get_tail_lines("How many lines to display? (e.g. 20): ")
    tail_limit = _parse_tail_count(tail_num_raw)

    console.print("\n(Press Ctrl+C to stop and return to menu.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, auto_refresh=False) as live:
                tracker = LiveFrameTracker(live, view_name="Non-Running Pods")
                while True:
                    tracker.show_loading()
                    pods, error, command_descriptor = get_pods(ns)
                    if error:
                        _handle_kubectl_fetch_error(
                            tracker,
                            command=command_descriptor,
                            context="fetching pod data",
                            detail=error,
                            snapshot_title="Non-Running Pod - Error",
                        )
                        tracker.tick()
                        continue

                    def _is_non_running(pod: AttrDict) -> bool:
                        phase = getattr(getattr(pod, "status", None), "phase", "")
                        return phase not in ("Running", "Succeeded")

                    filtered = [pod for pod in pods if _is_non_running(pod)]
                    if not filtered:
                        _render_empty_state(
                            tracker,
                            command_descriptor,
                            "Non-Running Pods",
                            "No matching pods found.",
                            suggestion="All pods are Running or Succeeded.",
                        )
                        tracker.tick()
                        continue

                    selected = _select_tail_sorted(
                        filtered,
                        tail_limit,
                        key=lambda pod: (
                            getattr(getattr(pod, "metadata", None), "name", "") or ""
                        ),
                    )

                    table = Table(
                        show_header=True, header_style="bold magenta", box=box.ROUNDED
                    )
                    include_namespace = ns is None
                    if include_namespace:
                        table.add_column(
                            "Namespace", style="bold green", overflow="fold"
                        )
                    table.add_column("Name", overflow="fold")
                    table.add_column("Phase")
                    table.add_column("Ready")
                    table.add_column("Restarts", justify="right")
                    table.add_column(_label_time_header("CreatedAt"))
                    if show_extra:
                        table.add_column("PodIP")
                        table.add_column("Node", overflow="fold")

                    markdown_rows: List[List[str]] = []
                    frame_parts: List[str] = []
                    for pod in selected:
                        metadata = getattr(pod, "metadata", None)
                        status = getattr(pod, "status", None)
                        spec = getattr(pod, "spec", None)

                        namespace = getattr(metadata, "namespace", "-") or "-"
                        name = getattr(metadata, "name", "-") or "-"
                        creation = _format_timestamp(
                            getattr(metadata, "creation_timestamp", None)
                        )
                        summary = _summarize_pod_containers(pod)
                        ready_display = _format_ready_ratio(
                            summary.ready,
                            summary.total,
                        )
                        restarts = summary.restarts
                        phase = getattr(status, "phase", "") or "-"
                        pod_ip = getattr(status, "pod_ip", "") or "-"
                        node_name = getattr(spec, "node_name", "") or "-"

                        row: List[str] = []
                        if include_namespace:
                            row.append(namespace)
                        row.extend(
                            [
                                name,
                                phase,
                                ready_display,
                                str(restarts),
                                creation,
                            ]
                        )
                        if show_extra:
                            row.extend([pod_ip, node_name])
                        table.add_row(*row, style=_row_style_for_status(phase, "pod"))

                        markdown_rows.append(row.copy())
                        frame_parts.append(
                            "|".join(
                                row
                                + (
                                    [
                                        namespace if include_namespace else "",
                                        pod_ip if show_extra else "",
                                        node_name if show_extra else "",
                                    ]
                                )
                            )
                        )

                    frame_key = _make_frame_key("data", *frame_parts)
                    headers = (
                        ["Namespace", "Name", "Phase", "Ready", "Restarts", "CreatedAt"]
                        if include_namespace
                        else ["Name", "Phase", "Ready", "Restarts", "CreatedAt"]
                    )
                    if show_extra:
                        headers.extend(["PodIP", "Node"])
                    headers = _label_time_headers(headers)
                    snapshot = _format_table_snapshot(
                        title="Non-Running Pod",
                        headers=headers,
                        rows=markdown_rows,
                        command=command_descriptor,
                        status="success",
                    )
                    structured_data = {"headers": headers, "rows": markdown_rows}
                    tracker.update(
                        frame_key,
                        _compose_group(command_descriptor, table),
                        snapshot,
                        structured_data=structured_data,
                        input_state=CURRENT_INPUT_DISPLAY,
                    )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\nReturning to menu...", style="bold yellow")


def watch_pod_counts() -> None:
    """
    5) Pod Monitoring - 전체/정상/비정상 Pod 개수 출력 (2초 간격)
       namespace 지정 가능
    """
    console.print("\n[5] Pod Count Summary", style="bold blue")
    ns = choose_namespace()
    console.print("\n(Press Ctrl+C to stop and return to menu.)", style="bold yellow")
    try:
        with suppress_terminal_echo():
            with Live(console=console, auto_refresh=False) as live:
                tracker = LiveFrameTracker(live, view_name="Pod Count Summary")
                while True:
                    tracker.show_loading()
                    pods, error, command_descriptor = get_pods(ns)
                    if error:
                        _handle_kubectl_fetch_error(
                            tracker,
                            command=command_descriptor,
                            context="fetching pod data",
                            detail=error,
                            snapshot_title="Pod Count Summary - Error",
                        )
                        tracker.tick()
                        continue
                    total = len(pods)
                    normal = sum(
                        1
                        for p in pods
                        if getattr(p.status, "phase", None) in ("Running", "Succeeded")
                    )
                    abnormal = total - normal

                    summary_lines = [
                        f"Total Pods    : {total}",
                        f"Normal Pods   : {normal}",
                        f"Abnormal Pods : {abnormal}",
                    ]
                    summary_text = "\n".join(summary_lines)
                    summary_panel = Panel(
                        Group(
                            Text("Pod Count Summary", style="bold blue"),
                            Text.from_markup(
                                f"[green]Total Pods    : {total}[/green]\n"
                                f"[green]Normal Pods   : {normal}[/green]\n"
                                f"[red]Abnormal Pods : {abnormal}[/red]"
                            ),
                        ),
                        border_style="blue",
                    )
                    frame_key = _make_frame_key(
                        "data",
                        command_descriptor,
                        str(total),
                        str(normal),
                        str(abnormal),
                    )
                    snapshot = _format_plain_snapshot(
                        SnapshotPayload(
                            title="Pod Count Summary",
                            status="info",
                            body=summary_text,
                            command=command_descriptor,
                        )
                    )
                    tracker.update(
                        frame_key,
                        _compose_group(command_descriptor, summary_panel),
                        snapshot,
                        input_state=CURRENT_INPUT_DISPLAY,
                    )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\nReturning to menu...", style="bold yellow")


def watch_node_monitoring_by_creation() -> None:
    """
    7) Node Monitoring (생성된 순서)
       AZ와 선택한 라벨(key=value) 정보를 함께 표시하며, 사용자 지정 라벨로 필터링 가능
    """
    console.print("\n[7] Node List (by creation)", style="bold blue")
    filter_choice = (
        Prompt.ask("Filter by node label (key=value)? (yes/no)", default="no")
        .strip()
        .lower()
    )
    label_selection = choose_node_group() if filter_choice.startswith("y") else None
    tail_num_raw = get_tail_lines("How many lines to display? (e.g. 20): ")
    tail_limit = _parse_tail_count(tail_num_raw)

    label_selector = label_selection.expression if label_selection else None
    label_column_key = label_selection.key if label_selection else NODE_GROUP_LABEL
    label_column_title = _label_column_title(label_column_key)
    console.print("\n(Press Ctrl+C to stop and return to menu.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, auto_refresh=False) as live:
                tracker = LiveFrameTracker(live, view_name="Node List (by creation)")
                while True:
                    tracker.show_loading()
                    nodes, error, command_descriptor = get_nodes(label_selector)
                    if error:
                        _handle_kubectl_fetch_error(
                            tracker,
                            command=command_descriptor,
                            context="fetching node data",
                            detail=error,
                            snapshot_title="Node List (by creation) - Error",
                        )
                        tracker.tick()
                        continue

                    if label_selection and label_selection.expression:
                        nodes = [
                            node
                            for node in nodes
                            if _node_label_value(node, label_selection.key)
                            == label_selection.value
                        ]

                    if not nodes:
                        _render_empty_state(
                            tracker,
                            command_descriptor,
                            "Node List (by creation)",
                            "No nodes to display.",
                            suggestion="Check label filter or cluster connectivity.",
                        )
                        tracker.tick()
                        continue

                    selected = _select_tail_sorted(
                        nodes,
                        tail_limit,
                        key=lambda node: (
                            _ensure_datetime(
                                getattr(
                                    getattr(node, "metadata", None),
                                    "creation_timestamp",
                                    None,
                                )
                            )
                            or UTC_MIN
                        ),
                    )

                    table = Table(
                        show_header=True, header_style="bold magenta", box=box.ROUNDED
                    )
                    table.add_column("Name", style="bold green", overflow="fold")
                    table.add_column("Status")
                    table.add_column(label_column_title, overflow="fold")
                    table.add_column("Zone")
                    table.add_column("Version")
                    table.add_column(_label_time_header("CreatedAt"))

                    markdown_rows: List[List[str]] = []
                    frame_parts: List[str] = []
                    for node in selected:
                        metadata = getattr(node, "metadata", None)
                        status = getattr(node, "status", None)
                        node_info = getattr(status, "node_info", None)

                        name = getattr(metadata, "name", "-") or "-"
                        ready_state = _node_ready_condition(node)
                        node_group = _node_label_value(node, label_column_key) or "-"
                        zone = _node_zone(node)
                        version = getattr(node_info, "kubelet_version", "") or "-"
                        created_at = _format_timestamp(
                            getattr(metadata, "creation_timestamp", None)
                        )

                        row = [
                            name,
                            ready_state,
                            node_group,
                            zone,
                            version,
                            created_at,
                        ]
                        table.add_row(
                            *row, style=_row_style_for_status(ready_state, "node")
                        )
                        markdown_rows.append(row.copy())
                        frame_parts.append("|".join(row))

                    frame_key = _make_frame_key("data", *frame_parts)
                    headers = _label_time_headers(
                        [
                            "Name",
                            "Status",
                            label_column_title,
                            "Zone",
                            "Version",
                            "CreatedAt",
                        ]
                    )
                    snapshot = _format_table_snapshot(
                        title="Node List (by creation)",
                        headers=headers,
                        rows=markdown_rows,
                        command=command_descriptor,
                        status="success",
                    )
                    structured_data = {"headers": headers, "rows": markdown_rows}
                    tracker.update(
                        frame_key,
                        _compose_group(command_descriptor, table),
                        snapshot,
                        structured_data=structured_data,
                        input_state=CURRENT_INPUT_DISPLAY,
                    )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\nReturning to menu...", style="bold yellow")


def watch_unhealthy_nodes() -> None:
    """
    8) Node Monitoring (Unhealthy Node 확인)
       AZ와 선택한 라벨(key=value) 정보를 함께 표시하며, 지정 라벨로 필터링 가능
    """
    console.print("\n[8] Unhealthy Nodes", style="bold blue")
    filter_choice = (
        Prompt.ask("Filter by node label (key=value)? (yes/no)", default="no")
        .strip()
        .lower()
    )
    label_selection = choose_node_group() if filter_choice.startswith("y") else None
    tail_num_raw = get_tail_lines("How many lines to display? (e.g. 20): ")
    tail_limit = _parse_tail_count(tail_num_raw)

    label_selector = label_selection.expression if label_selection else None
    label_column_key = label_selection.key if label_selection else NODE_GROUP_LABEL
    label_column_title = _label_column_title(label_column_key)
    console.print("\n(Press Ctrl+C to stop and return to menu.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, auto_refresh=False) as live:
                tracker = LiveFrameTracker(live, view_name="Unhealthy Nodes")
                while True:
                    tracker.show_loading()
                    nodes, error, command_descriptor = get_nodes(label_selector)
                    if error:
                        _handle_kubectl_fetch_error(
                            tracker,
                            command=command_descriptor,
                            context="fetching node data",
                            detail=error,
                            snapshot_title="Unhealthy Node - Error",
                        )
                        tracker.tick()
                        continue

                    if label_selection and label_selection.expression:
                        nodes = [
                            node
                            for node in nodes
                            if _node_label_value(node, label_selection.key)
                            == label_selection.value
                        ]

                    unhealthy = [
                        node for node in nodes if _node_ready_condition(node) != "Ready"
                    ]
                    if not unhealthy:
                        _render_empty_state(
                            tracker,
                            command_descriptor,
                            "Unhealthy Nodes",
                            "No unhealthy nodes found.",
                            suggestion="All nodes are in Ready state.",
                        )
                        tracker.tick()
                        continue

                    selected = _select_tail_sorted(
                        unhealthy,
                        tail_limit,
                        key=lambda node: (
                            getattr(getattr(node, "metadata", None), "name", "") or ""
                        ),
                    )

                    table = Table(
                        show_header=True, header_style="bold magenta", box=box.ROUNDED
                    )
                    table.add_column("Name", style="bold green", overflow="fold")
                    table.add_column("Status")
                    table.add_column("Reason")
                    table.add_column(label_column_title, overflow="fold")
                    table.add_column("Zone")
                    table.add_column("Version")
                    table.add_column(_label_time_header("CreatedAt"))

                    markdown_rows: List[List[str]] = []
                    frame_parts: List[str] = []
                    for node in selected:
                        metadata = getattr(node, "metadata", None)
                        status = getattr(node, "status", None)
                        node_info = getattr(status, "node_info", None)

                        name = getattr(metadata, "name", "-") or "-"
                        ready_state = _node_ready_condition(node)
                        reason = "-"
                        conditions = getattr(status, "conditions", None) or []
                        for condition in conditions:
                            if getattr(condition, "type", "") == "Ready":
                                reason = getattr(condition, "reason", "") or "-"
                                break
                        node_group = _node_label_value(node, label_column_key) or "-"
                        zone = _node_zone(node)
                        version = getattr(node_info, "kubelet_version", "") or "-"
                        created_at = _format_timestamp(
                            getattr(metadata, "creation_timestamp", None)
                        )

                        row = [
                            name,
                            ready_state,
                            reason,
                            node_group,
                            zone,
                            version,
                            created_at,
                        ]
                        table.add_row(
                            *row, style=_row_style_for_status(ready_state, "node")
                        )
                        markdown_rows.append(row.copy())
                        frame_parts.append("|".join(row))

                    frame_key = _make_frame_key("data", *frame_parts)
                    headers = _label_time_headers(
                        [
                            "Name",
                            "Status",
                            "Reason",
                            label_column_title,
                            "Zone",
                            "Version",
                            "CreatedAt",
                        ]
                    )
                    snapshot = _format_table_snapshot(
                        title="Unhealthy Node",
                        headers=headers,
                        rows=markdown_rows,
                        command=command_descriptor,
                        status="warning",
                    )
                    structured_data = {"headers": headers, "rows": markdown_rows}
                    tracker.update(
                        frame_key,
                        _compose_group(command_descriptor, table),
                        snapshot,
                        structured_data=structured_data,
                        input_state=CURRENT_INPUT_DISPLAY,
                    )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\nReturning to menu...", style="bold yellow")


def watch_node_resources() -> None:
    """
    9) Node Monitoring (CPU/Memory 사용량 높은 순 정렬)
       사용자 선택 라벨(key=value) 기준으로 필터링 가능 (기본: NODE_GROUP_LABEL)
    """
    console.print("\n[9] Node Resource Usage (CPU/Memory)", style="bold blue")
    while True:
        sort_key = Prompt.ask("Select sort key (1: CPU, 2: Memory)", choices=["1", "2"])
        if sort_key == "1":
            sort_column = 3  # CPU 열 인덱스
            break
        elif sort_key == "2":
            sort_column = 5  # Memory 열 인덱스
            break
        else:
            console.print("Invalid input. Please try again.", style="bold red")

    top_n = Prompt.ask("How many top nodes to display?", default="20")
    if not top_n.isdigit():
        console.print("Not a number. Using default: 20.", style="bold red")
        top_n = "20"

    filter_choice = (
        Prompt.ask("Filter by node label (key=value)? (yes/no)", default="no")
        .strip()
        .lower()
    )
    label_selection = choose_node_group() if filter_choice.startswith("y") else None
    selector_expression = label_selection.expression if label_selection else None

    if selector_expression:
        base_cmd = f"kubectl top node -l {selector_expression} --no-headers"
    else:
        base_cmd = "kubectl top node --no-headers"

    full_cmd = f"{base_cmd} | sort -k{sort_column} -nr 2>/dev/null | head -n {top_n}"
    console.print("\n(Press Ctrl+C to stop and return to menu.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, auto_refresh=False) as live:
                tracker = LiveFrameTracker(live, view_name="Node Resource Usage")
                while True:
                    tracker.show_loading()
                    stdout, error = _run_shell_command(full_cmd)
                    if error:
                        frame_key = ("error", (error,))
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Node Resource Top - Error",
                                status="error",
                                body=f"Command execution failed:\n{error}",
                                command=full_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                full_cmd,
                                Panel(
                                    f"Command execution failed:\n{error}",
                                    title="Error",
                                    style="bold red",
                                ),
                            ),
                            snapshot,
                            input_state=CURRENT_INPUT_DISPLAY,
                        )
                        tracker.tick()
                        continue
                    else:
                        output = stdout.rstrip()
                        if not output:
                            _render_empty_state(
                                tracker,
                                full_cmd,
                                "Node Resource Usage",
                                "No nodes to display.",
                            )
                            tracker.tick()
                            continue
                        else:
                            frame_key = ("data", (output,))
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Node Resource Top",
                                    status="success",
                                    body=output,
                                    command=full_cmd,
                                )
                            )
                            tracker.update(
                                frame_key,
                                _compose_group(
                                    full_cmd,
                                    Panel(
                                        output,
                                        title="Result",
                                        border_style="green",
                                    ),
                                ),
                                snapshot,
                                input_state=CURRENT_INPUT_DISPLAY,
                            )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\nReturning to menu...", style="bold yellow")


def watch_pod_resources() -> None:
    """
    6) Pod Monitoring (CPU/Memory 사용량 높은 순 정렬)
       namespace 선택 및 노드 라벨(key=value) 기반 필터링 지원
    """
    console.print("\n[6] Pod Resource Usage (CPU/Memory)", style="bold blue")
    namespace = choose_namespace()

    sort_key = Prompt.ask("Select sort key (1: CPU, 2: Memory)", choices=["1", "2"])
    top_n_raw = Prompt.ask("How many top pods to display?", default="20").strip()
    try:
        top_n = int(top_n_raw)
    except ValueError:
        console.print("Not a number. Using default: 20.", style="bold red")
        top_n = 20
    if top_n <= 0:
        console.print("Value must be positive. Using default: 20.", style="bold red")
        top_n = 20

    filter_choice = (
        Prompt.ask("Filter by node label (key=value)? (yes/no)", default="no")
        .strip()
        .lower()
    )
    label_selection = choose_node_group() if filter_choice.startswith("y") else None

    node_filter: Optional[Set[str]] = None
    if label_selection and label_selection.expression:
        node_filter = _collect_nodes_for_selector(label_selection)
        if not node_filter:
            console.print(
                f"No nodes found for label {label_selection}.",
                style="bold red",
            )
            return

    console.print("\n(Press Ctrl+C to stop and return to menu.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, auto_refresh=False) as live:
                tracker = LiveFrameTracker(live, view_name="Pod Resource Usage")
                while True:
                    tracker.show_loading()
                    if label_selection and label_selection.expression:
                        node_filter = _collect_nodes_for_selector(label_selection)
                        if not node_filter:
                            console.print(
                                "[bold red]No nodes match the label filter. Resetting filter.[/bold red]"
                            )
                            label_selection = None
                            node_filter = None

                    metrics, error, kubectl_cmd = _get_kubectl_top_pod(namespace)
                    if error:
                        frame_key = _make_frame_key("error", error)
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Pod Resource Top - Error",
                                status="error",
                                body=f"kubectl top pod failed:\n{error}",
                                command=kubectl_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                kubectl_cmd,
                                Panel(
                                    f"kubectl top pod failed:\n{error}",
                                    title="Error",
                                    style="bold red",
                                ),
                            ),
                            snapshot,
                            input_state=CURRENT_INPUT_DISPLAY,
                        )
                        tracker.tick()
                        continue

                    if not metrics:
                        _render_empty_state(
                            tracker,
                            kubectl_cmd,
                            "Pod Resource Usage",
                            "No pod metrics to display.",
                            suggestion="Ensure metrics-server is running in the cluster.",
                        )
                        tracker.tick()
                        continue

                    pod_to_node = _map_pod_to_node(namespace)

                    enriched = []
                    for ns_name, pod_name, cpu_raw, mem_raw in metrics:
                        node_name = pod_to_node.get((ns_name, pod_name), "-")
                        if node_filter and node_name not in node_filter:
                            continue
                        enriched.append(
                            {
                                "namespace": ns_name,
                                "pod": pod_name,
                                "cpu_raw": cpu_raw,
                                "cpu_millicores": _parse_cpu_to_millicores(cpu_raw),
                                "memory_raw": mem_raw,
                                "memory_bytes": _parse_memory_to_bytes(mem_raw),
                                "node": node_name,
                            }
                        )

                    if not enriched:
                        _render_empty_state(
                            tracker,
                            kubectl_cmd,
                            "Pod Resource Usage",
                            "No pods match the filter criteria.",
                            suggestion="Try broadening the node label filter.",
                        )
                        tracker.tick()
                        continue

                    if sort_key == "1":
                        enriched.sort(
                            key=lambda item: cast(int, item["cpu_millicores"]),
                            reverse=True,
                        )
                        subtitle = "Sorted by: CPU(cores)"
                    else:
                        enriched.sort(
                            key=lambda item: cast(int, item["memory_bytes"]),
                            reverse=True,
                        )
                        subtitle = "Sorted by: Memory(bytes)"

                    limited = enriched[:top_n]
                    header = Text(
                        f"=== Pod Resource Usage (Top {top_n}) ===  [{subtitle}]",
                        style="bold blue",
                    )
                    table = Table(
                        show_header=True, header_style="bold magenta", box=box.ROUNDED
                    )
                    table.add_column("Namespace", style="bold green", overflow="fold")
                    table.add_column("Pod", overflow="fold")
                    table.add_column("CPU(cores)")
                    table.add_column("Memory(bytes)")
                    table.add_column("Node", overflow="fold")

                    markdown_rows = []
                    for row in limited:
                        table.add_row(
                            str(row["namespace"]),
                            str(row["pod"]),
                            str(row["cpu_raw"]),
                            str(row["memory_raw"]),
                            str(row["node"]),
                        )
                        markdown_rows.append(
                            [
                                str(row["namespace"]),
                                str(row["pod"]),
                                str(row["cpu_raw"]),
                                str(row["memory_raw"]),
                                str(row["node"]),
                            ]
                        )

                    frame_key = _make_frame_key(
                        "data",
                        subtitle,
                        *(
                            f"{str(r['namespace'])}|{str(r['pod'])}|{str(r['cpu_raw'])}|{str(r['memory_raw'])}|{str(r['node'])}"
                            for r in limited
                        ),
                    )
                    snapshot = _format_table_snapshot(
                        title=f"Pod Resource Usage (Top {top_n}) - {subtitle}",
                        headers=[
                            "Namespace",
                            "Pod",
                            "CPU(cores)",
                            "Memory(bytes)",
                            "Node",
                        ],
                        rows=markdown_rows,
                        command=kubectl_cmd,
                        status="success",
                    )
                    structured_data = {
                        "headers": [
                            "Namespace",
                            "Pod",
                            "CPU(cores)",
                            "Memory(bytes)",
                            "Node",
                        ],
                        "rows": markdown_rows,
                    }
                    tracker.update(
                        frame_key,
                        _compose_group(kubectl_cmd, header, table),
                        snapshot,
                        structured_data=structured_data,
                        input_state=CURRENT_INPUT_DISPLAY,
                    )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\nReturning to menu...", style="bold yellow")


def main_menu() -> str:
    """메인 메뉴 출력."""
    context = _get_current_context()

    menu_table = Table(
        show_header=False,
        box=box.ROUNDED,
        padding=(0, 1),
        highlight=True,
        title="Kubernetes Monitoring Tool",
        title_style="bold yellow",
        title_justify="center",
        caption=f"Cluster: [bold cyan]{context}[/bold cyan]",
        caption_justify="center",
    )
    menu_table.add_column("Option", width=6)
    menu_table.add_column("Description", style="white")

    # Events & Containers
    menu_table.add_row("", "[bold cyan]Events & Containers[/bold cyan]")
    menu_table.add_row("[bold green]1[/bold green]", "Event Monitoring")
    menu_table.add_row("[bold green]2[/bold green]", "Container Restart Logs")
    menu_table.add_row()

    # Pods
    menu_table.add_row("", "[bold green]Pods[/bold green]")
    menu_table.add_row("[bold green]3[/bold green]", "Pod List (by creation)")
    menu_table.add_row("[bold green]4[/bold green]", "Non-Running Pods")
    menu_table.add_row("[bold green]5[/bold green]", "Pod Count Summary")
    menu_table.add_row("[bold green]6[/bold green]", "Pod Resource Usage (CPU/Memory)")
    menu_table.add_row()

    # Nodes
    menu_table.add_row("", "[bold blue]Nodes[/bold blue]")
    menu_table.add_row("[bold green]7[/bold green]", "Node List (by creation)")
    menu_table.add_row("[bold green]8[/bold green]", "Unhealthy Nodes")
    menu_table.add_row("[bold green]9[/bold green]", "Node Resource Usage (CPU/Memory)")
    menu_table.add_row()

    menu_table.add_row("[bold yellow]Q[/bold yellow]", "Quit")

    console.print(menu_table)
    return Prompt.ask("Select an option")


def main() -> None:
    """
    메인 함수 실행
    """
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, handle_winch)

    try:
        while True:
            choice = main_menu()
            if choice == "1":
                watch_event_monitoring()
            elif choice == "2":
                view_restarted_container_logs()
            elif choice == "3":
                watch_pod_monitoring_by_creation()
            elif choice == "4":
                watch_non_running_pod()
            elif choice == "5":
                watch_pod_counts()
            elif choice == "6":
                watch_pod_resources()
            elif choice == "7":
                watch_node_monitoring_by_creation()
            elif choice == "8":
                watch_unhealthy_nodes()
            elif choice == "9":
                watch_node_resources()
            elif choice.upper() == "Q":
                _exit_with_cleanup(0, "Exiting gracefully.", style="bold green")
            else:
                console.print(
                    "Invalid input. Please enter a number (1-9) or Q.",
                    style="bold yellow",
                )
    except KeyboardInterrupt:
        _exit_with_cleanup(
            130, "User interrupt (Ctrl+C) detected. Shutting down safely."
        )
    except EOFError:
        _exit_with_cleanup(
            0, "Input terminated (EOF). Exiting gracefully.", style="bold green"
        )


if __name__ == "__main__":
    main()
