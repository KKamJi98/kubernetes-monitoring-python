# monitoring-kubernetes

Kubernetes Monitoring Tool

## Overview

Kubernetes 클러스터에서 이벤트, Pod, Node 상태 등을 빠르게 확인할 수 있는 모니터링 툴입니다.
메뉴 선택 방식으로 다양한 정보를 조회할 수 있습니다.
**코드에 대한 개선점이나 필요한 기능이 있으면 언제든 문의 환영합니다. (Welcome!)**

### 주요 기능

1. **Event Monitoring**
   - 2초 간격으로 `kubectl get events`를 재실행해 최신 이벤트를 확인
   - 실행 명령은 내부적으로 추적하며 UI에는 이벤트 데이터만 표시

2. **Container Monitoring (재시작된 컨테이너 및 로그)**
   - 최근에 재시작된 컨테이너를 시간 기준으로 정렬하여 확인하고, 특정 컨테이너의 이전 로그(-p 옵션)를 확인

3. **Pod Monitoring**
   - 생성된 순서, Running이 아닌 Pod, 전체/정상/비정상 Pod 개수를 조회
   - CPU/Memory 사용량 기준 상위 Pod를 실시간으로 확인하며 NodeGroup 필터링 가능
   - Ready 지표는 `[ready/total]` 형태로 고정돼 스프레드시트에서 날짜로 변환되지 않음

4. **Node Monitoring**
   - 생성된 순서(노드 정보), Unhealthy Node, CPU/Memory 사용량이 높은 노드를 확인
   - 모든 뷰에서 NodeGroup(라벨 기반) 필터링을 지원하고, UI는 노드 지표만 출력

## Requirements

- **Python 3.9 이상**
  - 가상환경(pyenv, conda 또는 venv)을 사용하면 충돌을 줄이고 독립된 환경을 유지할 수 있음
- **필수 라이브러리**
  - [kubernetes](https://pypi.org/project/kubernetes/)
  - [rich](https://pypi.org/project/rich/)
- **uv** (패키지 관리자)
- **kubectl** (Kubernetes Client)

## Installation & Usage

### 1. Git Clone & Python 실행

1. **Repository Clone**

   ```shell
   git clone https://github.com/KKamJi98/monitoring-kubernetes.git
   cd monitoring-kubernetes
   ```

2. **라이브러리 설치**

   ```shell
   uv pip install .
   ```

   - Python 3.9 버전 이상의 환경에서 실행을 권장합니다.

3. **스크립트 실행**

   ```shell
   python kubernetes_monitoring.py
   ```

   - 메뉴가 표시되면 원하는 항목 번호(또는 Q)를 입력하여 사용할 수 있습니다.

### 2. 실행 파일로 등록하여 사용 (옵션)

1. **Repository Clone**

   ```shell
   git clone https://github.com/KKamJi98/monitoring-kubernetes.git
   cd monitoring-kubernetes
   ```

2. **라이브러리 설치**

   ```shell
   uv pip install .
   ```

3. **실행 권한 부여**

   ```shell
   chmod u+x kubernetes_monitoring.py
   ```

4. **경로 이동**

   ```shell
   sudo cp kubernetes_monitoring.py /usr/local/bin/kubernetes_monitoring.py
   ```

5. **실행**

   ```shell
   kubernetes_monitoring.py
   ```

> 참고: 일반적으로 `/usr/local/bin`은 기본적으로 `PATH`에 포함됩니다.
> 만약 `PATH`에 `/usr/local/bin`이 없다면, `~/.bashrc` 또는 `~/.zshrc`에 다음을 추가해야 합니다.

```shell
export PATH=$PATH:/usr/local/bin
```

#### 짧은 명령어로 사용하기 (Alias)

```shell
alias kmp="kubernetes_monitoring.py"

or

alias kmp="python -u /usr/local/bin/kubernetes_monitoring.py"
```

## NodeGroup 라벨 커스터마이징

- 스크립트 최상단에 있는 `NODE_GROUP_LABEL` 변수를 통해 NodeGroup 라벨 키를 쉽게 변경할 수 있습니다.
- 기본값은 `"node.kubernetes.io/app"`로 설정되어 있으며, EKS 환경에서 NodeGroup 구분 시 흔히 사용하는 라벨입니다.

```python
NODE_GROUP_LABEL = "node.kubernetes.io/app"
```

## Menu Description

스크립트 실행 시 아래와 같은 메뉴가 표시되며, 원하는 번호를 선택하여 기능을 사용할 수 있습니다.

```
Kubernetes Monitoring Tool
╭───┬───────────────────────────────────────────────────────────────────────────────────╮
│ 1 │ Event Monitoring (Normal, !=Normal)                                               │
│ 2 │ Container Monitoring (재시작된 컨테이너 및 로그)                                  │
│ 3 │ Pod Monitoring (생성된 순서) [옵션: Pod IP 및 Node Name 표시]                     │
│ 4 │ Pod Monitoring (Running이 아닌 Pod) [옵션: Pod IP 및 Node Name 표시]              │
│ 5 │ Pod Monitoring (전체/정상/비정상 Pod 개수 출력)                                   │
│ 6 │ Pod Monitoring (CPU/Memory 사용량 높은 순 정렬) [NodeGroup 필터링 가능]           │
│ 7 │ Node Monitoring (생성된 순서) [AZ, NodeGroup 표시 및 필터링 가능]                 │
│ 8 │ Node Monitoring (Unhealthy Node 확인) [AZ, NodeGroup 표시 및 필터링 가능]         │
│ 9 │ Node Monitoring (CPU/Memory 사용량 높은 순 정렬) [NodeGroup 필터링 가능]          │
│ Q │ Quit                                                                              │
╰───┴───────────────────────────────────────────────────────────────────────────────────╯
```

> 참고: Live 모니터링 중 입력한 키는 화면에 표시되지 않으며, 루프 종료 시 자동으로 버려집니다. 이를 통해 화면 밀림 없이 안정적으로 갱신됩니다.

#### 스냅샷 저장 (코드블록 + CSV)

- Live 화면에서 `s`, `:s`, `save`, `:save`, `:export` 중 하나를 입력하고 Enter를 누르면 현재 프레임을 코드블록 형태로 `/var/tmp/kmp/YYYY-MM-DD-HH-MM-SS.md` 경로에 저장하고, 동일한 이름의 `.csv` 파일을 동시에 생성합니다.
- `.md` 파일에는 화면의 표를 그대로 옮긴 텍스트 코드블록이 저장되며, 상태 메시지는 이탤릭 한 줄로 시작합니다.
- 예시:

  ```
  *:white_check_mark: Event Monitoring*

  Namespace  LastSeen (KST)       Type    Reason  Object                              Message
  ---------  -------------------  ------  ------  ----------------------------------  ---------------
  default    2025-10-13 14:33:58  Normal  Valid   ClusterSecretStore/parameter-store  store validated
  ```
- `.md` 파일에는 실행 명령이 포함되지 않으며, UI에서도 명령은 노출하지 않습니다.
- 시간 컬럼 헤더는 KST(UTC+09:00) 기준으로 표기되며, 예: `LastSeen (KST)`, `CreatedAt (KST)`
- `.csv` 파일은 동일한 데이터 집합을 구조화해 제공하며, 별도의 CSV 저장 명령(`csv`, `:csv`)도 동일 포맷으로 동작합니다.
- 저장이 완료되면 CLI 하단에 두 파일 경로가 표시됩니다. `/var/tmp/kmp`에 쓰기 권한이 없으면 저장이 실패하며, 오류 메시지를 통해 원인을 안내합니다.
- Live 모드 상단 `command input` 패널에서 `:` 프롬프트에 따라 입력 중인 문자열을 실시간으로 확인할 수 있어, `:save` 등 명령이 제대로 입력됐는지 즉시 파악할 수 있습니다.

### 1. Event Monitoring

- 전체 이벤트 혹은 `type!=Normal` 이벤트를 2초 간격으로 재조회하여 최신 상태를 확인
- tail -n [사용자 지정] 개수만큼 표시하며, 이벤트 본문만 갱신

### 2. Container Monitoring (재시작된 컨테이너 및 로그)

- 최근 재시작된 컨테이너의 종료 시점(`lastState.terminated.finishedAt`) 기준으로 내림차순 정렬 후, 목록에서 특정 컨테이너를 선택해 이전 로그(`kubectl logs -p`)를 확인
- tail -n [사용자 지정] 개수만큼 로그를 볼 수 있음

### 3. Pod Monitoring (생성된 순서)

- `kubectl get po ... --sort-by=.metadata.creationTimestamp` 명령을 2초 간격으로 실행하여 최신 생성 순서를 확인
- Ready 컬럼은 `[ready/total]` 형태로 노출되어 스프레드시트 자동 변환을 방지

### 4. Pod Monitoring (Running이 아닌 Pod 확인)

- `kubectl get pods ... | grep -ivE ' Running'` 명령을 2초 간격으로 실행해 Running이 아닌 Pod만 필터링
- Pod IP 및 Node Name 표시 옵션 제공, Ready 컬럼은 `[ready/total]` 형태로 제공

### 5. Pod Monitoring (전체/정상/비정상 Pod 개수)

- 2초 간격으로 전체 Pod 개수, 정상(Running 또는 Succeeded) Pod 개수, 비정상 Pod 개수를 표시
- API 호출 결과가 변할 때만 콘솔을 갱신해 깜빡임을 최소화

### 6. Pod Monitoring (CPU/Memory 사용량 높은 순 정렬)

- `kubectl top pod` 결과를 2초마다 조회하고 CPU/Memory 기준으로 정렬하여 상위 N개 Pod를 표시
- NodeGroup 라벨 기반 필터링을 지원하며, Ready 컬럼은 `[ready/total]` 형태로 출력

### 7. Node Monitoring (생성된 순서)

- 노드 생성 시간(`.metadata.creationTimestamp`) 기준으로 정렬된 목록을 2초마다 재조회
- Zone(`topology.ebs.csi.aws.com/zone`)와 NodeGroup(`NODE_GROUP_LABEL`)을 함께 출력

### 8. Node Monitoring (Unhealthy Node 확인)

- `kubectl get nodes ... | grep -ivE ' Ready '` 명령을 2초마다 실행하여 Ready가 아닌 노드만 필터링
- NodeGroup 필터링을 지원하며, 건강 상태와 라벨 정보를 함께 제공

### 9. Node Monitoring (CPU/Memory 사용량 높은 순 정렬)

- `kubectl top node` 결과를 2초마다 조회해 CPU 혹은 메모리 기준으로 정렬, 상위 N개 노드를 표시
- NodeGroup 라벨 기반 필터링을 지원하며, 리소스 사용량 지표만 출력

## Development

- 환경 설정(uv 권장):

  ```shell
  uv venv
  source .venv/bin/activate
  uv pip install .
  uv pip install ".[dev]"
  ```

- 포매팅(ruff):

  ```shell
  ruff format .
  ```

- 린트(ruff):

  ```shell
  ruff check .
  ```

- 타입 체크(mypy):

  ```shell
  mypy .
  ```

- 테스트(pytest):

  ```shell
  pytest --cov=./ --cov-report=xml
  ```

> 스타일 가이드: 본 프로젝트는 ruff(포매터+린터), mypy, pytest를 사용합니다. 모든 체크 통과 후에만 커밋/푸시합니다.
