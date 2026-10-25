# GitHub 반영 전 테스트 계획

## 2026-10-25 경쟁형 v1.3 검증

기준과 API는 [경쟁형 연결 계약](COMPETITIVE_V1_3.md)을 따른다. 아래 기존 계획은
`mode=legacy` 회귀 범위이며, 경쟁형의 강제 정보량·자유 응답·GM 책임과 혼용하지 않는다.

| 범위 | 실행 | 통과 기준 |
| --- | --- | --- |
| 경쟁형 규칙·격리·장애 | `python -B -m unittest discover -q` | 기존 90개와 경쟁형 50개; 단일 GM·옛 임기 차단·재시작·미션 58종·추리 동시 판정·응답 진실성·비밀 격리 |
| 경쟁형 화면 | `python -B tests/run_competition_browser.py` | 캐릭터 12명, 방향성 관계 그래프, 운영자 정지/재개, 관전자 비밀 차단, 모바일, JS 오류 없음 |
| 기존 화면·HTTP | `python -B tests/run_browser.py`, `python -B tests/run_external.py` | 기존 게임·승계·외부 대역의 두 회차 실행 유지 |
| C 정책 | `python -B -m unittest discover -s teams/GM_Agent_Society -t teams -p test_v12_policy.py -q` | 최신 C 순수 정책 23개 통과 |
| B 기본 호환 | `python -B tests/probe_team_player.py` | 실제 B 클래스 + 모델 스텁으로 기존 계약의 관찰·행동·질문 응답 검사 |
| 관계 효과 | `python -B tests/relationship_experiment.py` | 같은 난수로 조건당 1,024회 비교; 효과 OFF일 때 관계값별 성공률 동일, ON일 때 순서 변화 |
| 경쟁형 공동 실행 | `python -B launcher.py --seed 7 --progress` | 실제 HTTP·C 순수 정책·명시적 스크립트 플레이어/합법성 어댑터로 공식 종료·REPORT |
| 장애 공동 실행 | launcher의 `--scenario player-fault`, `gm-fault`, `human` | 중간 보충 없음, GM 승계·옛 자격 차단, 예비 GM의 0명 종료; 전원 소진 운영자 종료는 단위 테스트 |

실측 관계 질문 성공률은 관계 20/50/80에서 28.81%/57.62%/77.54%, 효과 OFF에서는
모두 57.62%였다. 이는 엔진 규칙의 효과만 검증하며 LLM의 관계 이해·학습 효과를 뜻하지 않는다.
최초 관계는 확정 명세대로 매 회차 50으로 초기화한다. 사전 친분·회차 간 관계 누적은 넣지 않았다.

실제 B의 경쟁형 런타임과 유료 LLM 호출은 이 검증에 포함되지 않는다. 실행기 결과에는
`live_llm=false`, 사용한 정책/어댑터, 필터링 수, 저장소 버전과 실행 출처를 명시한다.
후속 B/C 인계 시 성공 단계별 응답, 역할 전환, 출력 토큰 집계까지 실제 런타임으로 추가 검증한다.

2026-10-25 실행 결과: 전체 140개와 C 정책 23개 통과. 두 브라우저 검사, 기존 HTTP 대역,
B 스텁, 관계 실험 모두 통과. 경쟁형 실제 HTTP 정상 실행은 120사이클 마감으로 종료했고,
플레이어 장애 실행은 53사이클에 승자가 나왔다(질문 82회, 정보 전달 20회, 관계 갱신 82회).
GM 장애 실행은 로컬 동일 API 드라이버에서 48사이클 승자·승계 1회를 확인했다.
예비 GM의 0명 종료는 실제 HTTP로 확인했다. 장애 실행은 종료에 성공해도 공식 REPORT의
`technical_failure=true`, `success=false`를 유지한다. 대역의 게임 전략 성능을 합격 조건으로 삼지 않는다.

대상: `internal_docs` 요구에 따른 멀티에이전트 기능과 기존 1인용 게임의 회귀 여부.

## 외부 연결 준비 v1 추가 통과 기준

내장 데모와 외부 B/C 계약을 분리한다. [외부 연결 계약](INTEGRATION_CONTRACT.md)과
`integration_schema.json`이 전달 기준이며, 아래 검증은 실제 B/C 자율 실행을 대신하지 않는다.

| 항목 | 검증 | 통과 기준 |
| --- | --- | --- |
| 영속 풀·회차 분리 | `test_integration.py` | 고유 agent_id, 중복 예약 거부, 재시작 복원, 새 game에서 비밀 초기화 |
| 외부 정책 책임 | 동일 | create는 GM만 선발; 내장 정책 비실행; 외부 PLAN만 활성화/배분/대안 기록 |
| map·단계·task | 동일 | CAS, 순환 의존 거부·전체 롤백, 단계 1회 소비, generation 별도 검증 |
| 정상 질문 응답 | 동일 | 수신자 동의/거절, 허용 증거만 확정, 부분 가림, 응답 추가 소비 0, timeout·재배정·지연 결과 차단 |
| 가시성·자료 검색 | 동일 | GM 기본 ID-only, 자기/공유 자료만 조회, 개인 증거/목표/미발견 정답 미노출 |
| 역할 승격·장애 | 동일 | 같은 풀원 GM 승격, 원래 캐릭터 NPC, 옛 player/GM 자격 차단, 동시 선출 한 명 |
| 운영자 | 동일 | pause/resume와 기한 보존, 후보 없음/한도 초과 대기, 후임 지정/종료, GM 없어도 CLI 조회 |
| 공식 피드백·전략 | 동일 | 엔진 결과만 역량 갱신, 자기 공식 경험+CAS 전략 갱신, raw 비밀 저장 거부 |
| 실제 HTTP 대역 시연 | `python -B tests/run_external.py` | 풀→계획→직접 행동/질문→판단 입력 변화→승격→두 번째 game에서 ID/전략 유지 |
| 기존 기능 회귀 | 기존 테스트 및 브라우저 | 기존 /, /multi의 정상·장애·저장·UI 시나리오 유지 |

```powershell
python -B -m unittest discover -v
python -B tests/run_external.py
python -B tests/run_browser.py
git diff --check
git check-ignore teams/GM_Agent_Society/README.md teams/Agent_Reasoning/deploy/agent.py
```

외부 HTTP 시연은 스크립트 대역이며 LLM/API 비용·B/C 실제 실행·학습 향상을 검증하지 않는다.
JSON Schema는 와이어 구조, 실제 권한·소유권·동시성 규칙은 엔진 테스트가 검증한다.

`python -B tests/probe_team_player.py`는 선택 검사다. 최신 B d06c650의 실제 LLMAgent를
native flat entrypoint로 실행하고 모델 응답만 대역으로 제공한다. canonical 후보/profile,
2개 행동 반환값, 정상 질문 응답, 근거 반영을 확인하지만 C 전체 연결을 검증하지 않는다.

### 외부 연결 준비 실행 결과 (2026-10-12)

- 전체 unittest **90개 모두 통과**: 기존 36개 + 외부 계약 54개.
- `tests/run_external.py`: 실제 HTTP에서 영속 ID·본인 역할 session·외부 계획/map·직접 행동·
  거절/동의/관찰 변화·승격과 옛 자격 차단·공식 피드백·다음 게임 상태 분리 통과.
- `tests/probe_team_player.py`: B d06c650 native LLMAgent + 모델 대역의 관찰·2개 반환·응답·신념 반영 통과.
- 기존 브라우저 E2E와 `git diff --check` 통과. teams/ 및 기존 설치 환경의 Git 제외 확인.
- C 확인 기준은 f00bc05. B 저장소는 원격 d06c650으로 fast-forward만 했고 B/C 소스 수정은 없음.
- 실제 B/C 전체 자율 실행·유료 LLM·학습 성능 향상·H1~H3 통계 검증은 미실행.

## 통과 기준

| 항목 | 검증 방법 | 통과 기준 |
| --- | --- | --- |
| 기존 1인용 게임 | `test_game.py` | 단서 25개·정답 보존, 판정/비용, 대기 판정 우회 방지, 저장 복원, API 어댑터 테스트 전부 통과 |
| 가변 인원·재현 | `test_multi.py` | 1/3/12명 생성, 동일 seed의 사건·태그 일치, 잘못된 인원 거부 |
| 정보·권한 경계 | `test_multi.py` | GM/다른 플레이어/모델 요청에 숨겨진 정답·타인의 개인 상태·인증 토큰 미노출, 잘못된 자격 거부 |
| 행동 계약 | `test_multi.py` | 동일 행동 ID 재실행 방지, 변경 요청 거부, 실패한 담당자의 지연 결과 거부, 부분/전체 공유·비공개 메시지 범위 유지 |
| 장애 복구 | `test_multi.py` | 실행 중 3→2→1명 감소 시 기존 공유 증거 보존, 미완료 작업 재배정, 최종 해결; 0명일 때 사람 개입 대기 |
| 비동기 진행 | `test_multi.py` | 한 플레이어가 응답 대기 중이어도 다른 플레이어가 두 번 이상 행동 가능 |
| GM 에이전트 장애 | `test_coordination.py` | GM 예외·무응답을 엔진이 감지, 이전 GM NPC 전환, 후임 GM으로 최종 해결 |
| 단일 컨트롤 타워 | `test_coordination.py` | 동시 선출에서 한 후임만 확정, 임기·token·lease 검증, 이전 GM의 지연 명령 및 cached 명령 거부 |
| GM 감독·인계 | `test_coordination.py` | 후임이 소유 ID 원장과 공유 상태를 복원하며 개인 본문·목표·정답은 읽지 못함 |
| 런타임 재시작 | `test_coordination.py` | 이전 실행자 기한 만료 후 새 임기 선출, 저장된 완료 결과를 보존하고 자동 해결 |
| HTTP 통합·영속성 | `test_multi.py` | 사건 생성·백그라운드 실행·관찰·개인 조회·공개 내보내기 성공, SQLite 저장 복원 |
| 브라우저 E2E | `python tests/run_browser.py` | 화면 이동·생성·장애 주입·1명 해결·개인 조회·공개 다운로드·새로고침 복원 성공, 모바일 가로 넘침/JS 오류 없음 |
| Git 변경 범위 | `git diff --check`, `git check-ignore`, staged 목록 검토 | 공백 오류 없음; 키·DB·내부 문서·설치 환경 미포함 |

필수 항목 중 하나라도 실패하면 push하지 않습니다. 수정이 필요하면 해당 항목과 전체 자동 테스트를 다시 실행합니다.

## 실행

```powershell
python -m unittest discover -v
python tests/run_browser.py
git diff --check
git check-ignore .playwright/node_modules/node/bin/node.exe .playwright/shared-export.json .playwright/mobile.png
```

브라우저 검증은 별도 임시 SQLite와 임의의 localhost 포트를 사용하고 종료 시 서버/워커를 정리합니다. 사용자 게임 데이터와 API 키를 사용하지 않습니다. 테스트 코드만 추적하며 설치 패키지·다운로드·스크린샷은 `.playwright/`에 저장해 Git에서 제외합니다.

Windows에서는 설치된 Microsoft Edge를 사용합니다. 다른 환경은 Playwright Chromium 설치 또는 `BROWSER_EXECUTABLE` 경로가 필요합니다. 검증 환경 준비 예:

```powershell
npm --prefix .playwright install node@22 playwright --no-audit --no-fund
```

## 별도 실서비스 검증

실제 유료 LLM 호출은 이번 push의 필수 기준에 포함하지 않습니다. API 어댑터·모델 오류 대체·관찰 범위는 mock으로 검증합니다. 실제 키의 유효성, 요금/한도, 모델 응답 품질은 키를 입력한 후 별도 확인해야 합니다. 이 테스트는 연구 가설의 통계적 검증을 대신하지 않습니다.

## 기존 내장 데모 실행 결과 (2026-10-12, 이전 패치 기준)

- `python -m unittest discover -v`: 36개 모두 통과 (기존 24개 + GM 승계/권한/종료/복구 12개).
- `python tests/run_browser.py`: 화면 이동, 사건 생성, GM 승계·NPC 전환·단일 지휘자, 두 명 장애, 작업 재배정, 한 명으로 해결, 개인 조회, 안전한 공개 내보내기, 새로고침 복원, 모바일 레이아웃 및 JS 오류 검사 통과.
- 실제 유료 API 호출은 미실행이며, 이번 반영의 필수 통과 범위에서 제외했습니다.
