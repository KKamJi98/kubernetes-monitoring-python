# monitoring-kubernetes

Kubernetes Monitoring Tool

## Overview

Kubernetes 클러스터에서 이벤트, Pod, Node 상태 등을 빠르게 확인할 수 있는 모니터링 툴입니다.
메뉴 선택 방식으로 다양한 정보를 조회할 수 있습니다.
**코드에 대한 개선점이나 필요한 기능이 있으면 언제든 문의 환영합니다. (Welcome!)**

### 주요 기능

1. **Event Monitoring**
   - 전체 이벤트 혹은 정상(Normal)이 아닌 이벤트만 실시간(`watch`)으로 모니터링

2. **Container Monitoring (재시작된 컨테이너 및 로그)**
   - 최근에 재시작된 컨테이너를 시간 기준으로 정렬하여 확인하고, 특정 컨테이너의 이전 로그(-p 옵션)를 확인

3. **Pod Monitoring**
   - 생성된 순서, Running이 아닌 Pod, 전체/정상/비정상 Pod 개수를 조회

4. **Node Monitoring**
   - 생성된 순서(노드 정보), Unhealthy Node, CPU/Memory 사용량이 높은 노드를 확인
   - NodeGroup(라벨 기반)으로 필터링 가능

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
│ 6 │ Node Monitoring (생성된 순서) [AZ, NodeGroup 표시 및 필터링 가능]                 │
│ 7 │ Node Monitoring (Unhealthy Node 확인) [AZ, NodeGroup 표시 및 필터링 가능]         │
│ 8 │ Node Monitoring (CPU/Memory 사용량 높은 순 정렬) [NodeGroup 필터링 가능]          │
│ Q │ Quit                                                                              │
╰───┴───────────────────────────────────────────────────────────────────────────────────╯
```

### 1. Event Monitoring

- 전체 이벤트 혹은 `type!=Normal` 이벤트를 실시간(`watch -n2`)으로 확인
- 최신 이벤트부터 tail -n [사용자 지정] 개수로 표시

### 2. Container Monitoring (재시작된 컨테이너 및 로그)

- 최근 재시작된 컨테이너의 종료 시점(`lastState.terminated.finishedAt`) 기준으로 내림차순 정렬 후, 목록에서 특정 컨테이너를 선택해 이전 로그(`kubectl logs -p`)를 확인
- tail -n [사용자 지정] 개수만큼 로그를 볼 수 있음

### 3. Pod Monitoring (생성된 순서)

- Pod 생성 시간(`.metadata.creationTimestamp`) 기준 정렬
- tail -n [사용자 지정] 개수 표시

### 4. Pod Monitoring (Running이 아닌 Pod 확인)

- `kubectl get pods` 결과에서 `grep -ivE 'Running'` 조건으로 Running이 아닌 Pod만 필터링
- Pod IP 및 Node Name 확인 옵션

### 5. Pod Monitoring (전체/정상/비정상 Pod 개수)

- 2초 간격으로 전체 Pod 개수, 정상(Running 또는 Succeeded) Pod 개수, 비정상 Pod 개수를 표시

### 6. Node Monitoring (생성된 순서)

- 노드의 생성 시간(`.metadata.creationTimestamp`) 기준 정렬
- Zone(`topology.ebs.csi.aws.com/zone`)와 NodeGroup(`NODE_GROUP_LABEL`)을 표시 및 필터링 가능

### 7. Node Monitoring (Unhealthy Node 확인)

- `kubectl get nodes` 결과에서 `grep -ivE ' Ready'`로 Ready가 아닌 노드만 필터링

### 8. Node Monitoring (CPU/Memory 사용량 높은 순 정렬)

- `kubectl top node` 결과에서 CPU나 메모리 기준으로 정렬 후 상위 N개 표시
- NodeGroup 라벨 기반 필터링 가능 (`-l node.kubernetes.io/app=<값>`)

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
