#!/usr/bin/env python3

import contextlib
import datetime
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

try:
    from kubernetes import client, config
    from kubernetes.client import CoreV1Api, V1NamespaceList, V1Pod
except ImportError:
    client = None  # type: ignore
    config = None  # type: ignore
    CoreV1Api = None  # type: ignore
    V1Pod = None  # type: ignore
from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

console = Console()

# 노드그룹 라벨을 변수로 분리 (기본값: node.kubernetes.io/app)
NODE_GROUP_LABEL = "node.kubernetes.io/app"

SNAPSHOT_EXPORT_DIR = Path("/var/tmp/kmp")
SNAPSHOT_SAVE_COMMANDS = {"s", ":s", "save", ":save", ":export"}

WINDOWS_INPUT_BUFFER: List[str] = []
POSIX_INPUT_BUFFER: List[str] = []
CURRENT_INPUT_DISPLAY = ""


def _set_input_display(text: str) -> None:
    global CURRENT_INPUT_DISPLAY
    CURRENT_INPUT_DISPLAY = text


def _clear_input_display() -> None:
    _set_input_display("")


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


def _status_icon(status: str) -> Optional[str]:
    mapping = {
        "success": ":white_check_mark:",
        "error": ":x:",
        "warning": ":warning:",
        "info": ":information_source:",
        "empty": ":information_source:",
    }
    return mapping.get(status, None)


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

    if payload.command:
        lines.append("*Command*")
        lines.extend(["```", payload.command.strip(), "```"])
    return "\n".join(lines)


def _format_table_snapshot(
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    command: str,
    status: str = "info",
) -> str:
    """테이블 데이터를 Slack Markdown 표로 변환."""
    sanitized_headers = [header.strip() for header in headers]
    sanitized_rows: List[str] = []
    for row in rows:
        sanitized_cells = []
        for cell in row:
            sanitized_cells.append(str(cell).replace("\n", " ").strip())
        sanitized_rows.append("| " + " | ".join(sanitized_cells) + " |")

    icon = _status_icon(status)
    title_line = f"*{title.strip()}*"
    if icon:
        title_line = f"*{icon} {title.strip()}*"

    contents = [
        title_line,
        "| " + " | ".join(sanitized_headers) + " |",
        "| " + " | ".join(["---"] * len(sanitized_headers)) + " |",
        *sanitized_rows,
    ]
    contents.append("*Command*")
    contents.extend(["```", command.strip(), "```"])
    return "\n".join(contents)


def _save_markdown_snapshot(markdown: str) -> Path:
    """슬랙 공유용 Markdown 파일을 저장하고 경로 반환."""
    try:
        SNAPSHOT_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotSaveError(SNAPSHOT_EXPORT_DIR, exc) from exc
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    file_path = SNAPSHOT_EXPORT_DIR / f"{timestamp}.md"
    try:
        file_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        raise SnapshotSaveError(file_path, exc) from exc
    return file_path


def _read_nonblocking_command() -> Optional[str]:
    """사용자 입력(line 단위)을 블로킹 없이 읽어 Slack 저장 요청 감지."""
    if not sys.stdin.isatty():
        return None

    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            return None

        command_ready = None
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                command_ready = "".join(WINDOWS_INPUT_BUFFER).strip()
                WINDOWS_INPUT_BUFFER.clear()
                _clear_input_display()
                # Enter 입력 시 CR/LF 잔여 문자를 비움
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

    command_ready: Optional[str] = None
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
            command_ready = "".join(POSIX_INPUT_BUFFER).strip()
            POSIX_INPUT_BUFFER.clear()
            _clear_input_display()
            break
        if char in ("\x7f", "\b"):
            if POSIX_INPUT_BUFFER:
                POSIX_INPUT_BUFFER.pop()
            _set_input_display("".join(POSIX_INPUT_BUFFER))
            continue
        POSIX_INPUT_BUFFER.append(char)
        _set_input_display("".join(POSIX_INPUT_BUFFER))
    if command_ready:
        return command_ready
    return None


def _handle_snapshot_command(
    live: Live, markdown: Optional[str], command: Optional[str]
) -> None:
    """저장 요청이 있는 경우 Markdown을 파일로 기록."""
    if command is None:
        return
    display_command = command.strip() or "<empty>"
    normalized = command.lower()
    if normalized not in SNAPSHOT_SAVE_COMMANDS:
        live.console.print(
            f"\n입력 '{display_command}' 은(는) 지원하지 않는 명령입니다. "
            "사용 가능한 입력: s, :s, save, :save, :export",
            style="bold yellow",
        )
        return
    if not markdown:
        live.console.print(
            f"\n입력 '{display_command}' 처리 실패: 저장할 데이터가 없습니다.",
            style="bold yellow",
        )
        return
    try:
        path = _save_markdown_snapshot(markdown)
    except SnapshotSaveError as exc:
        live.console.print(
            f"\n입력 '{display_command}' 처리 실패: {exc}",
            style="bold red",
        )
        _clear_input_display()
        return
    live.console.print(
        f"\n입력 '{display_command}' 처리 성공: Slack Markdown 스냅샷 저장 완료 → {path}",
        style="bold green",
    )
    _clear_input_display()


def _tick_iteration(live: Live, markdown: Optional[str]) -> None:
    """루프 종료 전 저장 요청을 처리."""
    user_command = _read_nonblocking_command()
    if user_command:
        _handle_snapshot_command(live, markdown, user_command)


class LiveFrameTracker:
    """Live 갱신 및 스냅샷 생성을 추적하는 도우미."""

    def __init__(self, live: Live) -> None:
        self.live = live
        self.last_frame: Optional[Tuple[str, Tuple[str, ...]]] = None
        self.latest_snapshot: Optional[str] = None
        self.last_input_state: str = ""

    def update(
        self,
        frame_key: Tuple[str, Tuple[str, ...]],
        renderable: RenderableType,
        snapshot_markdown: Optional[str],
        input_state: Optional[str] = None,
    ) -> None:
        current_input_state = (
            input_state if input_state is not None else CURRENT_INPUT_DISPLAY
        )
        if current_input_state != self.last_input_state:
            self.last_input_state = current_input_state
            self.last_frame = None
        if frame_key != self.last_frame:
            self.live.update(renderable)
            self.last_frame = frame_key
        if snapshot_markdown is not None:
            self.latest_snapshot = snapshot_markdown

    def tick(self) -> None:
        _tick_iteration(self.live, self.latest_snapshot)
        time.sleep(2)


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


def _command_panel(command: str) -> Panel:
    """공통적으로 사용하는 kubectl 명령 패널 생성."""
    return Panel(
        Text(f"$ {command}", style="bold cyan"),
        title="kubectl command",
        border_style="cyan",
    )


class _CommandInputRenderable:
    """현재 입력 버퍼 상태를 실시간으로 렌더링."""

    def __rich_console__(self, console: Console, options):  # type: ignore[override]
        display = CURRENT_INPUT_DISPLAY
        if display:
            prompt = display if display.startswith(":") else f":{display}"
            style = "bold cyan"
        else:
            prompt = ":"
            style = "dim"
        yield Text(prompt, style=style)


def _input_prompt_panel() -> Panel:
    """현재 명령 입력 상태를 표시하는 패널."""
    return Panel(
        _CommandInputRenderable(),
        title="command input",
        border_style="cyan",
    )


def _compose_group(command: str, *renderables: RenderableType) -> Group:
    """메인 콘텐츠와 kubectl 명령을 하단에 배치한 그룹 구성."""
    items: List[RenderableType] = []
    items.append(_input_prompt_panel())
    items.extend(renderables)
    items.append(_command_panel(command))
    return Group(*items)


@contextlib.contextmanager
def suppress_terminal_echo() -> Iterator[None]:
    """Live 화면 동안 터미널 입력 에코를 비활성화."""
    if not sys.stdin.isatty():
        yield
        return

    if os.name == "nt":
        import ctypes
        import msvcrt

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
            while msvcrt.kbhit():
                msvcrt.getwch()
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


def _map_pod_to_node(
    v1_api: CoreV1Api, namespace: Optional[str] = None
) -> Dict[Tuple[str, str], str]:
    """Pod -> Node 매핑을 생성."""
    try:
        if namespace:
            pods = v1_api.list_namespaced_pod(namespace=namespace).items
        else:
            pods = v1_api.list_pod_for_all_namespaces().items
    except Exception:
        return {}
    mapping: Dict[Tuple[str, str], str] = {}
    for pod in pods:
        ns_name = getattr(pod.metadata, "namespace", None)
        pod_name = getattr(pod.metadata, "name", None)
        node_name = getattr(getattr(pod, "spec", None), "node_name", None)
        if ns_name and pod_name and node_name:
            mapping[(ns_name, pod_name)] = node_name
    return mapping


def _collect_nodes_for_group(v1_api: CoreV1Api, node_group: str) -> Set[str]:
    """선택한 NodeGroup에 속한 노드 이름 집합을 반환."""
    try:
        nodes = v1_api.list_node(
            label_selector=f"{NODE_GROUP_LABEL}={node_group}"
        ).items
    except Exception:
        return set()
    return {
        node.metadata.name for node in nodes if node.metadata and node.metadata.name
    }


def cleanup() -> None:
    """리소스 정리 및 종료 전 후처리.

    현재는 외부 리소스를 별도로 잡지 않지만, 추후 확장을 대비해
    공통 정리 지점을 한곳으로 모읍니다.
    """
    # 필요한 경우, 추가 정리 작업을 이곳에 배치합니다.
    console.print("정리 중...", style="dim")


def _exit_with_cleanup(code: int, message: str, style: str = "bold yellow") -> None:
    """메시지를 출력하고 정리 후 지정된 코드로 종료."""
    # 요구사항: 메시지 앞에 한 줄 공백 출력
    print()
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
    except Exception:
        return

    loop = asyncio.get_event_loop()

    stop_event = asyncio.Event()

    def _handle_signal(sig: int) -> None:
        console.print(
            f"신호 수신: {signal.Signals(sig).name}. 안전 종료를 시작합니다.",
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


def load_kube_config() -> None:
    """kube config 로드 (예외처리 포함)"""
    try:
        config.load_kube_config()
    except Exception as e:
        print(f"Error loading kube config: {e}")
        sys.exit(1)


def choose_namespace() -> Optional[str]:
    """
    클러스터의 모든 namespace 목록을 표시하고, 사용자가 index로 선택
    아무 입력도 없으면 전체(namespace 전체) 조회
    """
    load_kube_config()
    v1 = client.CoreV1Api()
    try:
        ns_list: V1NamespaceList = v1.list_namespace()
        items = ns_list.items
    except Exception as e:
        print(f"Error fetching namespaces: {e}")
        return None

    if not items:
        print("Namespace가 존재하지 않습니다.")
        return None

    table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
    table.add_column("Index", style="bold green", width=5)
    table.add_column("Namespace")
    for idx, ns in enumerate(items, start=1):
        table.add_row(str(idx), ns.metadata.name)
    console.print("\n=== Available Namespaces ===", style="bold green")
    console.print(table)

    selection = Prompt.ask(
        "조회할 Namespace 번호를 선택하세요 (기본값: 전체)", default=""
    )
    if not selection:
        return None
    if not selection.isdigit():
        print("숫자로 입력해주세요. 전체 조회로 진행합니다.")
        return None
    index = int(selection)
    if index < 1 or index > len(items):
        print("유효하지 않은 번호입니다. 전체 조회로 진행합니다.")
        return None
    chosen_ns = str(items[index - 1].metadata.name)
    return chosen_ns


def choose_node_group() -> Optional[str]:
    """
    클러스터의 모든 노드 그룹 목록(NODE_GROUP_LABEL로부터) 표시 후, 사용자가 index로 선택
    아무 입력도 없으면 필터링하지 않음
    """
    load_kube_config()
    v1 = client.CoreV1Api()
    try:
        nodes = v1.list_node().items
    except Exception as e:
        print(f"Error fetching nodes: {e}")
        return None

    node_groups: List[str] = []
    temp_node_groups = set()
    for node in nodes:
        if node.metadata.labels and NODE_GROUP_LABEL in node.metadata.labels:
            temp_node_groups.add(node.metadata.labels[NODE_GROUP_LABEL])
    node_groups = sorted(list(temp_node_groups))
    if not node_groups:
        print("노드 그룹이 존재하지 않습니다.")
        return None
    table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
    table.add_column("Index", style="bold green", width=5)
    table.add_column("Node Group")
    for idx, ng in enumerate(node_groups, start=1):
        table.add_row(str(idx), ng)
    console.print("\n=== Available Node Groups ===", style="bold green")
    console.print(table)

    selection = Prompt.ask(
        "필터링할 Node Group 번호를 선택하세요 (기본값: 필터링하지 않음)", default=""
    )
    if not selection:
        return None
    if not selection.isdigit():
        print("숫자로 입력해주세요. 필터링하지 않음으로 진행합니다.")
        return None
    index = int(selection)
    if index < 1 or index > len(node_groups):
        print("유효하지 않은 번호입니다. 필터링하지 않음으로 진행합니다.")
        return None
    chosen_ng = node_groups[index - 1]
    return chosen_ng


def get_tail_lines(prompt="몇 줄씩 확인할까요? (숫자 입력. default: 20줄): ") -> str:
    """
    tail -n에 사용할 숫자 입력 (기본값 20)
    """
    val = Prompt.ask(prompt, default="20")
    if val.isdigit():
        return val
    else:
        console.print("숫자로 입력해주세요.", style="bold red")
        return "20"


def get_pods(v1_api: CoreV1Api, namespace: Optional[str] = None) -> List[V1Pod]:
    """
    지정된 namespace 또는 전체 namespace에서 Pod 목록을 가져옵니다.
    """
    try:
        if namespace:
            return list(v1_api.list_namespaced_pod(namespace=namespace).items)
        else:
            return list(v1_api.list_pod_for_all_namespaces().items)
    except Exception as e:
        print(f"Error fetching pods: {e}")
        return []


def watch_event_monitoring() -> None:
    """
    1) Event Monitoring
       전체 이벤트 또는 비정상 이벤트(!=Normal)를 확인
    """
    console.print("\n[1] Event Monitoring", style="bold blue")
    ns = choose_namespace()
    event_choice = Prompt.ask(
        "어떤 이벤트를 보시겠습니까? (1: 전체 이벤트(default), 2: 비정상 이벤트(!=Normal))",
        default="1",
    )
    tail_num = get_tail_lines("몇 줄씩 확인할까요? (예: 20): ")

    ns_option = f"-n {ns}" if ns else "-A"
    if event_choice == "2":
        base_cmd = (
            f"kubectl get events {ns_option} "
            '--field-selector type!=Normal --sort-by=".metadata.managedFields[].time"'
        )
    else:
        base_cmd = (
            f'kubectl get events {ns_option} --sort-by=".metadata.managedFields[].time"'
        )
    full_cmd = f"{base_cmd} | tail -n {tail_num}"
    console.print("\n(Ctrl+C로 중지 후 메뉴로 돌아갑니다.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, refresh_per_second=4.0) as live:
                tracker = LiveFrameTracker(live)
                while True:
                    stdout, error = _run_shell_command(full_cmd)
                    if error:
                        frame_key = ("error", (error,))
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Event Monitoring - Error",
                                status="error",
                                body=f"명령 실행에 실패했습니다:\n{error}",
                                command=full_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                full_cmd,
                                Panel(
                                    f"명령 실행에 실패했습니다:\n{error}",
                                    title="오류",
                                    style="bold red",
                                ),
                            ),
                            snapshot,
                            input_state=CURRENT_INPUT_DISPLAY,
                        )
                        tracker.tick()
                        continue

                    output = stdout.rstrip()
                    if not output:
                        frame_key = ("empty", ())
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Event Monitoring - Empty",
                                status="empty",
                                body="표시할 이벤트가 없습니다.",
                                command=full_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                full_cmd,
                                Panel(
                                    "표시할 이벤트가 없습니다.",
                                    title="정보",
                                    style="bold yellow",
                                ),
                            ),
                            snapshot,
                            input_state=CURRENT_INPUT_DISPLAY,
                        )
                        tracker.tick()
                        continue

                    frame_key = ("data", (output,))
                    snapshot = _format_plain_snapshot(
                        SnapshotPayload(
                            title="Event Monitoring",
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
        console.print("\n메뉴로 돌아갑니다...", style="bold yellow")


def view_restarted_container_logs() -> None:
    """
    2) Container Monitoring (재시작된 컨테이너 및 로그)
       최근 재시작된 컨테이너 목록에서 선택하여 이전 컨테이너의 로그 확인
    """
    console.print("\n[2] 재시작된 컨테이너 확인 및 로그 조회", style="bold blue")
    load_kube_config()
    v1 = client.CoreV1Api()
    ns = choose_namespace()
    pods = get_pods(v1, ns)
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
    line_count = int(get_tail_lines("몇 개의 컨테이너를 표시할까요? (예: 20): "))
    displayed_containers = restarted_containers[:line_count]

    if not displayed_containers:
        print("최근 재시작된 컨테이너가 없습니다.")
        return

    table = Table(show_header=True, header_style="bold magenta", box=box.ROUNDED)
    table.add_column("INDEX", style="bold green", width=5)
    table.add_column("Namespace")
    table.add_column("Pod")
    table.add_column("Container")
    table.add_column("LastTerminatedTime")
    for i, (ns_pod, p_name, c_name, fat) in enumerate(displayed_containers, start=1):
        table.add_row(str(i), ns_pod, p_name, c_name, fat.strftime("%Y-%m-%d %H:%M:%S"))
    console.print(
        f"\n=== 최근 재시작된 컨테이너 목록 (시간 기준, Top {line_count}) ===\n",
        style="bold green",
    )
    console.print(table)

    sel = Prompt.ask("\n로그를 볼 INDEX를 입력 (Q: 종료)", default="").strip()
    if sel.upper() == "Q" or not sel.isdigit():
        return
    idx = int(sel)
    if idx < 1 or idx > len(displayed_containers):
        console.print("인덱스 범위를 벗어났습니다.", style="bold red")
        return
    ns_pod, p_name, c_name, _ = displayed_containers[idx - 1]
    log_tail = Prompt.ask(
        "몇 줄의 로그를 확인할까요? (미입력 시 50줄)", default="50"
    ).strip()
    if not log_tail.isdigit():
        console.print(
            "입력하신 값이 숫자가 아닙니다. 50줄을 출력합니다.", style="bold red"
        )
        log_tail = "50"
    cmd = f"kubectl logs -n {ns_pod} -p {p_name} -c {c_name} --tail={log_tail}"
    print(f"\n실행 명령어: {cmd}\n")
    os.system(cmd)


def watch_pod_monitoring_by_creation() -> None:
    """
    3) Pod Monitoring (생성된 순서)
       Pod IP 및 Node Name을 선택적으로 표시하며, namespace 지정 가능
    """
    console.print("\n[3] Pod Monitoring (생성된 순서)", style="bold blue")
    ns = choose_namespace()
    extra = (
        Prompt.ask("Pod IP 및 Node Name을 표시할까요? (yes/no)", default="no")
        .strip()
        .lower()
    )
    tail_num = get_tail_lines("몇 줄씩 확인할까요? (예: 20): ")
    ns_option = f"-n {ns}" if ns else "-A"
    if extra.startswith("y"):
        base_cmd = (
            f"kubectl get po {ns_option} -o wide --sort-by=.metadata.creationTimestamp"
        )
    else:
        base_cmd = f"kubectl get po {ns_option} --sort-by=.metadata.creationTimestamp"
    full_cmd = f"{base_cmd} | tail -n {tail_num}"
    console.print("\n(Ctrl+C로 중지 후 메뉴로 돌아갑니다.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, refresh_per_second=4.0) as live:
                tracker = LiveFrameTracker(live)
                while True:
                    stdout, error = _run_shell_command(full_cmd)
                    if error:
                        frame_key = ("error", (error,))
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Pod Monitoring (생성 순) - Error",
                                status="error",
                                body=f"명령 실행에 실패했습니다:\n{error}",
                                command=full_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                full_cmd,
                                Panel(
                                    f"명령 실행에 실패했습니다:\n{error}",
                                    title="오류",
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
                            frame_key = ("empty", ())
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Pod Monitoring (생성 순) - Empty",
                                    status="empty",
                                    body="표시할 결과가 없습니다.",
                                    command=full_cmd,
                                )
                            )
                            tracker.update(
                                frame_key,
                                _compose_group(
                                    full_cmd,
                                    Panel(
                                        "표시할 결과가 없습니다.",
                                        title="정보",
                                        style="bold yellow",
                                    ),
                                ),
                                snapshot,
                                input_state=CURRENT_INPUT_DISPLAY,
                            )
                            tracker.tick()
                            continue
                        else:
                            frame_key = ("data", (output,))
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Pod Monitoring (생성 순)",
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
        console.print("\n메뉴로 돌아갑니다...", style="bold yellow")


def watch_non_running_pod() -> None:
    """
    4) Pod Monitoring (Running이 아닌 Pod)
       Pod IP 및 Node Name을 선택적으로 표시하며, namespace 지정 가능
    """
    console.print("\n[4] Pod Monitoring (Running이 아닌 Pod)", style="bold blue")
    ns = choose_namespace()
    extra = (
        Prompt.ask("Pod IP 및 Node Name을 표시할까요? (yes/no)", default="no")
        .strip()
        .lower()
    )
    tail_num = get_tail_lines("몇 줄씩 확인할까요? (예: 20): ")
    ns_option = f"-n {ns}" if ns else "-A"
    if extra.startswith("y"):
        base_cmd = f"kubectl get pods {ns_option} -o wide"
    else:
        base_cmd = f"kubectl get pods {ns_option}"
    full_cmd = f"{base_cmd} | grep -ivE ' Running' | tail -n {tail_num}"
    console.print("\n(Ctrl+C로 중지 후 메뉴로 돌아갑니다.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, refresh_per_second=4.0) as live:
                tracker = LiveFrameTracker(live)
                while True:
                    stdout, error = _run_shell_command(full_cmd)
                    if error:
                        frame_key = ("error", (error,))
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Non-Running Pod - Error",
                                status="error",
                                body=f"명령 실행에 실패했습니다:\n{error}",
                                command=full_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                full_cmd,
                                Panel(
                                    f"명령 실행에 실패했습니다:\n{error}",
                                    title="오류",
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
                            frame_key = ("empty", ())
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Non-Running Pod - Empty",
                                    status="empty",
                                    body="조건에 해당하는 Pod가 없습니다.",
                                    command=full_cmd,
                                )
                            )
                            tracker.update(
                                frame_key,
                                _compose_group(
                                    full_cmd,
                                    Panel(
                                        "조건에 해당하는 Pod가 없습니다.",
                                        title="정보",
                                        style="bold yellow",
                                    ),
                                ),
                                snapshot,
                                input_state=CURRENT_INPUT_DISPLAY,
                            )
                            tracker.tick()
                            continue
                        else:
                            frame_key = ("data", (output,))
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Non-Running Pod",
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
        console.print("\n메뉴로 돌아갑니다...", style="bold yellow")


def watch_pod_counts() -> None:
    """
    5) Pod Monitoring - 전체/정상/비정상 Pod 개수 출력 (2초 간격)
       namespace 지정 가능
    """
    console.print(
        "\n[5] Pod Monitoring (전체/정상/비정상 Pod 개수 출력)", style="bold blue"
    )
    ns = choose_namespace()
    console.print("\n(Ctrl+C로 중지 후 메뉴로 돌아갑니다.)", style="bold yellow")
    load_kube_config()
    v1 = client.CoreV1Api()
    try:
        with suppress_terminal_echo():
            with Live(console=console, refresh_per_second=4.0) as live:
                tracker = LiveFrameTracker(live)
                while True:
                    pods = get_pods(v1, ns)
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
                    command_descriptor = (
                        "Python client: CoreV1Api.list_namespaced_pod"
                        if ns
                        else "Python client: CoreV1Api.list_pod_for_all_namespaces"
                    )
                    frame_key = (
                        "data",
                        (
                            command_descriptor,
                            str(total),
                            str(normal),
                            str(abnormal),
                        ),
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
        console.print("\n메뉴로 돌아갑니다...", style="bold yellow")


def watch_node_monitoring_by_creation() -> None:
    """
    7) Node Monitoring (생성된 순서)
       AZ, NodeGroup 정보를 함께 표시하며, 사용자가 특정 NodeGroup으로 필터링 가능
    """
    console.print("\n[7] Node Monitoring (생성된 순서)", style="bold blue")
    filter_choice = (
        Prompt.ask("특정 NodeGroup으로 필터링 하시겠습니까? (yes/no)", default="no")
        .strip()
        .lower()
    )
    if filter_choice.startswith("y"):
        filter_nodegroup = choose_node_group() or ""
    else:
        filter_nodegroup = ""
    tail_num = get_tail_lines("몇 줄씩 확인할까요? (예: 20): ")

    if filter_nodegroup:
        base_cmd = (
            f"kubectl get nodes -l {NODE_GROUP_LABEL}={filter_nodegroup} "
            f"-L topology.ebs.csi.aws.com/zone -L {NODE_GROUP_LABEL} "
            "--sort-by=.metadata.creationTimestamp"
        )
    else:
        base_cmd = (
            "kubectl get nodes "
            "-L topology.ebs.csi.aws.com/zone -L {label} "
            "--sort-by=.metadata.creationTimestamp"
        ).format(label=NODE_GROUP_LABEL)
    full_cmd = f"{base_cmd} | tail -n {tail_num}"
    console.print("\n(Ctrl+C로 중지 후 메뉴로 돌아갑니다.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, refresh_per_second=4.0) as live:
                tracker = LiveFrameTracker(live)
                while True:
                    stdout, error = _run_shell_command(full_cmd)
                    if error:
                        frame_key = ("error", (error,))
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Node Monitoring (생성 순) - Error",
                                status="error",
                                body=f"명령 실행에 실패했습니다:\n{error}",
                                command=full_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                full_cmd,
                                Panel(
                                    f"명령 실행에 실패했습니다:\n{error}",
                                    title="오류",
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
                            frame_key = ("empty", ())
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Node Monitoring (생성 순) - Empty",
                                    status="empty",
                                    body="표시할 노드가 없습니다.",
                                    command=full_cmd,
                                )
                            )
                            tracker.update(
                                frame_key,
                                _compose_group(
                                    full_cmd,
                                    Panel(
                                        "표시할 노드가 없습니다.",
                                        title="정보",
                                        style="bold yellow",
                                    ),
                                ),
                                snapshot,
                                input_state=CURRENT_INPUT_DISPLAY,
                            )
                            tracker.tick()
                            continue
                        else:
                            frame_key = ("data", (output,))
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Node Monitoring (생성 순)",
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
        console.print("\n메뉴로 돌아갑니다...", style="bold yellow")


def watch_unhealthy_nodes() -> None:
    """
    8) Node Monitoring (Unhealthy Node 확인)
       AZ, NodeGroup 정보를 함께 표시하며, 특정 NodeGroup 필터링 가능
    """
    console.print("\n[8] Node Monitoring (Unhealthy Node 확인)", style="bold blue")
    filter_choice = (
        Prompt.ask("특정 NodeGroup으로 필터링 하시겠습니까? (yes/no)", default="no")
        .strip()
        .lower()
    )
    if filter_choice.startswith("y"):
        filter_nodegroup = choose_node_group() or ""
    else:
        filter_nodegroup = ""
    tail_num = get_tail_lines("몇 줄씩 확인할까요? (예: 20): ")

    # label selector를 사용해서 정확한 노드 그룹으로 필터링
    if filter_nodegroup:
        base_cmd = (
            f"kubectl get nodes -l {NODE_GROUP_LABEL}={filter_nodegroup} "
            f"-L topology.ebs.csi.aws.com/zone -L {NODE_GROUP_LABEL} "
            "--sort-by=.metadata.creationTimestamp"
        )
    else:
        base_cmd = (
            "kubectl get nodes "
            "-L topology.ebs.csi.aws.com/zone -L {label} "
            "--sort-by=.metadata.creationTimestamp"
        ).format(label=NODE_GROUP_LABEL)

    full_cmd = f"{base_cmd} | grep -ivE ' Ready ' | tail -n {tail_num}"
    console.print("\n(Ctrl+C로 중지 후 메뉴로 돌아갑니다.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, refresh_per_second=4.0) as live:
                tracker = LiveFrameTracker(live)
                while True:
                    stdout, error = _run_shell_command(full_cmd)
                    if error:
                        frame_key = ("error", (error,))
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Unhealthy Node - Error",
                                status="error",
                                body=f"명령 실행에 실패했습니다:\n{error}",
                                command=full_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                full_cmd,
                                Panel(
                                    f"명령 실행에 실패했습니다:\n{error}",
                                    title="오류",
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
                            frame_key = ("empty", ())
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Unhealthy Node - Empty",
                                    status="empty",
                                    body="Unhealthy 노드가 없습니다.",
                                    command=full_cmd,
                                )
                            )
                            tracker.update(
                                frame_key,
                                _compose_group(
                                    full_cmd,
                                    Panel(
                                        "Unhealthy 노드가 없습니다.",
                                        title="정보",
                                        style="bold yellow",
                                    ),
                                ),
                                snapshot,
                                input_state=CURRENT_INPUT_DISPLAY,
                            )
                            tracker.tick()
                            continue
                        else:
                            frame_key = ("data", (output,))
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Unhealthy Node",
                                    status="warning",
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
        console.print("\n메뉴로 돌아갑니다...", style="bold yellow")


def watch_node_resources() -> None:
    """
    9) Node Monitoring (CPU/Memory 사용량 높은 순 정렬) 특정 NodeGroup 기준으로 필터링 가능
       NODE_GROUP_LABEL 변수 사용
    """
    console.print(
        "\n[9] Node Monitoring (CPU/Memory 사용량 높은 순 정렬)", style="bold blue"
    )
    while True:
        sort_key = Prompt.ask(
            "정렬 기준을 선택하세요 (1: CPU, 2: Memory)", choices=["1", "2"]
        )
        if sort_key == "1":
            sort_column = 3  # CPU 열 인덱스
            break
        elif sort_key == "2":
            sort_column = 5  # Memory 열 인덱스
            break
        else:
            console.print("잘못된 입력입니다. 다시 입력해주세요.", style="bold red")

    top_n = Prompt.ask("상위 몇 개 노드를 볼까요?", default="20")
    if not top_n.isdigit():
        console.print("숫자가 아닙니다. 기본값 20을 적용합니다.", style="bold red")
        top_n = "20"

    filter_choice = (
        Prompt.ask("특정 NodeGroup으로 필터링 하시겠습니까? (yes/no)", default="no")
        .strip()
        .lower()
    )
    filter_nodegroup = ""
    if filter_choice.startswith("y"):
        filter_nodegroup = choose_node_group() or ""

    if filter_nodegroup:
        base_cmd = (
            f"kubectl top node -l {NODE_GROUP_LABEL}={filter_nodegroup} --no-headers"
        )
    else:
        base_cmd = "kubectl top node --no-headers"

    full_cmd = f"{base_cmd} | sort -k{sort_column} -nr 2>/dev/null | head -n {top_n}"
    console.print("\n(Ctrl+C로 중지 후 메뉴로 돌아갑니다.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, refresh_per_second=4.0) as live:
                tracker = LiveFrameTracker(live)
                while True:
                    stdout, error = _run_shell_command(full_cmd)
                    if error:
                        frame_key = ("error", (error,))
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Node Resource Top - Error",
                                status="error",
                                body=f"명령 실행에 실패했습니다:\n{error}",
                                command=full_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                full_cmd,
                                Panel(
                                    f"명령 실행에 실패했습니다:\n{error}",
                                    title="오류",
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
                            frame_key = ("empty", ())
                            snapshot = _format_plain_snapshot(
                                SnapshotPayload(
                                    title="Node Resource Top - Empty",
                                    status="empty",
                                    body="표시할 노드가 없습니다.",
                                    command=full_cmd,
                                )
                            )
                            tracker.update(
                                frame_key,
                                _compose_group(
                                    full_cmd,
                                    Panel(
                                        "표시할 노드가 없습니다.",
                                        title="정보",
                                        style="bold yellow",
                                    ),
                                ),
                                snapshot,
                                input_state=CURRENT_INPUT_DISPLAY,
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
        console.print("\n메뉴로 돌아갑니다...", style="bold yellow")


def watch_pod_resources() -> None:
    """
    6) Pod Monitoring (CPU/Memory 사용량 높은 순 정렬)
       namespace 선택 및 NodeGroup 기준 필터링 지원
    """
    console.print(
        "\n[6] Pod Monitoring (CPU/Memory 사용량 높은 순 정렬)", style="bold blue"
    )
    namespace = choose_namespace()

    sort_key = Prompt.ask(
        "정렬 기준을 선택하세요 (1: CPU, 2: Memory)", choices=["1", "2"]
    )
    top_n_raw = Prompt.ask("상위 몇 개의 Pod를 볼까요?", default="20").strip()
    try:
        top_n = int(top_n_raw)
    except ValueError:
        console.print("숫자가 아닙니다. 기본값 20을 적용합니다.", style="bold red")
        top_n = 20
    if top_n <= 0:
        console.print(
            "0 이하 값은 허용되지 않습니다. 20을 적용합니다.", style="bold red"
        )
        top_n = 20

    filter_choice = (
        Prompt.ask("특정 NodeGroup으로 필터링 하시겠습니까? (yes/no)", default="no")
        .strip()
        .lower()
    )
    filter_nodegroup = ""
    if filter_choice.startswith("y"):
        filter_nodegroup = choose_node_group() or ""

    load_kube_config()
    v1 = client.CoreV1Api()
    node_filter: Optional[Set[str]] = None
    if filter_nodegroup:
        node_filter = _collect_nodes_for_group(v1, filter_nodegroup)
        if not node_filter:
            console.print(
                "선택한 NodeGroup에 해당하는 노드가 없습니다.", style="bold red"
            )
            return

    console.print("\n(Ctrl+C로 중지 후 메뉴로 돌아갑니다.)", style="bold yellow")

    try:
        with suppress_terminal_echo():
            with Live(console=console, refresh_per_second=4.0) as live:
                tracker = LiveFrameTracker(live)
                while True:
                    metrics, error, kubectl_cmd = _get_kubectl_top_pod(namespace)
                    if error:
                        frame_key = ("error", (error,))
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Pod Resource Top - Error",
                                status="error",
                                body=f"kubectl top pod 호출에 실패했습니다:\n{error}",
                                command=kubectl_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                kubectl_cmd,
                                Panel(
                                    f"kubectl top pod 호출에 실패했습니다:\n{error}",
                                    title="오류",
                                    style="bold red",
                                ),
                            ),
                            snapshot,
                            input_state=CURRENT_INPUT_DISPLAY,
                        )
                        tracker.tick()
                        continue

                    if not metrics:
                        frame_key = ("empty_metrics", ())
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Pod Resource Top - Empty",
                                status="empty",
                                body="표시할 Pod metrics가 없습니다.",
                                command=kubectl_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                kubectl_cmd,
                                Panel(
                                    "표시할 Pod metrics가 없습니다.",
                                    title="정보",
                                    style="bold yellow",
                                ),
                            ),
                            snapshot,
                            input_state=CURRENT_INPUT_DISPLAY,
                        )
                        tracker.tick()
                        continue

                    pod_to_node = _map_pod_to_node(v1, namespace)

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
                        frame_key = ("empty_filter", ())
                        snapshot = _format_plain_snapshot(
                            SnapshotPayload(
                                title="Pod Resource Top - Filter Empty",
                                status="empty",
                                body="필터 조건에 해당하는 Pod가 없습니다.",
                                command=kubectl_cmd,
                            )
                        )
                        tracker.update(
                            frame_key,
                            _compose_group(
                                kubectl_cmd,
                                Panel(
                                    "필터 조건에 해당하는 Pod가 없습니다.",
                                    title="정보",
                                    style="bold yellow",
                                ),
                            ),
                            snapshot,
                            input_state=CURRENT_INPUT_DISPLAY,
                        )
                        tracker.tick()
                        continue

                    if sort_key == "1":
                        enriched.sort(
                            key=lambda item: item["cpu_millicores"], reverse=True
                        )
                        subtitle = "정렬 기준: CPU(cores)"
                    else:
                        enriched.sort(
                            key=lambda item: item["memory_bytes"], reverse=True
                        )
                        subtitle = "정렬 기준: Memory(bytes)"

                    limited = enriched[:top_n]
                    header = Text(
                        f"=== Pod Resource Usage (Top {top_n}) ===  [{subtitle}]",
                        style="bold blue",
                    )
                    table = Table(
                        show_header=True, header_style="bold magenta", box=box.ROUNDED
                    )
                    table.add_column("Namespace", style="bold green")
                    table.add_column("Pod")
                    table.add_column("CPU(cores)")
                    table.add_column("Memory(bytes)")
                    table.add_column("Node")

                    markdown_rows = []
                    for row in limited:
                        table.add_row(
                            row["namespace"],
                            row["pod"],
                            row["cpu_raw"],
                            row["memory_raw"],
                            row["node"],
                        )
                        markdown_rows.append(
                            [
                                row["namespace"],
                                row["pod"],
                                row["cpu_raw"],
                                row["memory_raw"],
                                row["node"],
                            ]
                        )

                    frame_key = (
                        "data",
                        (
                            subtitle,
                            *(
                                f"{r['namespace']}|{r['pod']}|{r['cpu_raw']}|{r['memory_raw']}|{r['node']}"
                                for r in limited
                            ),
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
                    tracker.update(
                        frame_key,
                        _compose_group(kubectl_cmd, header, table),
                        snapshot,
                        input_state=CURRENT_INPUT_DISPLAY,
                    )
                    tracker.tick()
    except KeyboardInterrupt:
        console.print("\n메뉴로 돌아갑니다...", style="bold yellow")


def main_menu() -> str:
    """
    메인 메뉴 출력
    """
    menu_table = Table(
        show_header=False,
        box=box.ROUNDED,
        padding=(0, 1),
        highlight=True,
        title="Kubernetes Monitoring Tool",
        title_style="bold yellow",
        title_justify="center",
    )
    menu_table.add_column("Option")
    menu_table.add_column("Description", style="white")

    menu_options = [
        ("1", "Event Monitoring (Normal, !=Normal)"),
        ("2", "Container Monitoring (재시작된 컨테이너 및 로그)"),
        ("3", "Pod Monitoring (생성된 순서) [옵션: Pod IP 및 Node Name 표시]"),
        ("4", "Pod Monitoring (Running이 아닌 Pod) [옵션: Pod IP 및 Node Name 표시]"),
        ("5", "Pod Monitoring (전체/정상/비정상 Pod 개수 출력)"),
        (
            "6",
            "Pod Monitoring (CPU/Memory 사용량 높은 순 정렬) [NodeGroup 필터링 가능]",
        ),
        ("7", "Node Monitoring (생성된 순서) [AZ, NodeGroup 표시 및 필터링 가능]"),
        (
            "8",
            "Node Monitoring (Unhealthy Node 확인) [AZ, NodeGroup 표시 및 필터링 가능]",
        ),
        (
            "9",
            "Node Monitoring (CPU/Memory 사용량 높은 순 정렬) [NodeGroup 필터링 가능]",
        ),
        ("Q", "Quit"),
    ]

    for option, description in menu_options:
        if option == "Q":
            menu_table.add_row(f"[bold yellow]{option}[/bold yellow]", description)
        else:
            menu_table.add_row(f"[bold green]{option}[/bold green]", description)
    console.print(menu_table)
    return Prompt.ask("Select an option")


def main() -> None:
    """
    메인 함수 실행
    """
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
                _exit_with_cleanup(0, "정상 종료합니다.", style="bold green")
            else:
                print("잘못된 입력입니다. 메뉴에 표시된 숫자 또는 Q를 입력하세요.")
    except KeyboardInterrupt:
        _exit_with_cleanup(130, "사용자 중단(Ctrl+C) 감지: 안전하게 종료합니다.")
    except EOFError:
        _exit_with_cleanup(
            0, "입력이 종료되었습니다(EOF). 정상 종료합니다.", style="bold green"
        )


if __name__ == "__main__":
    main()
