# 외부 에이전트 연결 계약 v1

상태: **엔진 연결 준비용 구현**. 실제 B/C 전체 실행·LLM 학습·연구 가설 검증 완료를 뜻하지 않는다.
근거: 2026-10-12 개정 통합 회의안건과 수업자료 정합성 문서의 A 담당 범위.
회의 문서의 미확정 선택지는 아래 설정 가능한 기본값이며 팀 합의 완료로 간주하지 않는다.

## 실행 계층과 책임

`/`는 기존 1인용, `/multi`는 기존 내장 정책 데모다. 외부 프로젝트는
**`/integration/v1/`만 사용한다**. `kind=external` 게임에는 내장 플레이어·GM 정책이
실행되지 않는다. 엔진 watchdog는 만료 감지·NPC 전환·권한 승계만 수행한다.

- A: 영속 풀/ID, 배치, 공식 규칙·상태, 접근 권한, 결과/피드백, GM 선발·승계, 운영자 개입.
- B: 관찰→판단→직접 행동, 공개/거절, 개인 신념, 허용된 전략 상태 갱신.
- C: 활성 인원·직업·성향·task·의존성·대안·선택 이유를 제출하는 GM 역할 정책.
- 공통 런타임: 같은 agent_id를 재활성화하고 각 역할의 credential/관찰/맥락을 분리한다.

서비스: `integration.ExternalEngine`; HTTP 클라이언트: `engine_client.EngineClient`.
외부 패키지·팀 저장소 import 없이 Python 3.10+로 실행한다.
와이어 객체 명세는 [integration_schema.json](integration_schema.json)의 `$defs`를 사용한다.
JSON Schema는 구조 설명이며, 단계·권한·소유권·상태 검증은 엔진 코드가 집행한다.

```powershell
python app.py --no-browser
python integration_cli.py credentials
python -B tests/run_external.py
```

`credentials`는 DB 파일에 접근할 수 있는 **운영자만** 실행한다. 기본 DB는 `data/games.sqlite3`.
동일 DB를 사용하려면 `--data` 경로를 서버와 CLI에 동일하게 준다.
서버는 operator credential을 API로 공개하지 않는다. `X-GM-Client: local`은 인증이
아니며, 모든 비공개 작업에는 역할별 token이 추가로 필요하다. localhost 시연용이며
계정·TLS·인터넷 서비스용 인증은 별도다. DB에는 비밀/credential이 있으므로 공유하지 않는다.

## 미확정 사항의 구현 기본값

| 항목 | 기본값 / 조정 방법 |
| --- | --- |
| 활성 인원 | GM 1명 + 플레이어 1~12명. min_players/max_players로 초기 팀 구성 한도 변경. 장애 후 최소 인원 미만도 진행 가능; 0명만 대기. 대화 시연은 총 3명. GM이 PLAN의 activate로 선택 |
| 단계 | movement → interaction → deduction. GM이 전원 완료 후 ADVANCE. 한 단계당 능동 행동 1회 |
| 비용 | 실패·pass도 소비. 질문자는 등록 때 1회 예약/소비, pending 동안 장벽 유지. 응답·공유는 추가 소비 없음 |
| 판정 | 기본 stat=4, 1d6, 준비는 +1d. compassion > stat, brutality < stat, 동률 실패. 직업은 기본 능력 차이를 강제하지 않음 |
| GM 가시성 | `gm_visibility=ids_only`: 소유·획득 ID와 공유 내용만. `acquired_content`는 발견된 감독 본문까지 명시적으로 허용 |
| GM 선발 | cold start seed 탐색. 이후 공식 GM episode 결과의 평활 성공률 + `exploration_rate=.2`, 동률 seed 선택 |
| GM 승계 | `successor_scope=active_players`: 유효 lease의 플레이어를 승격하고 원래 캐릭터는 원자적으로 NPC화. `active_then_pool`로 가용 풀 후임 허용 가능 |
| 장애 | GM/player lease 40초, 질문 기한 30초. 반복 자동 승계 3회 초과 또는 후보 없음 → waiting_for_human |
| 0명 | 마지막 플레이어가 GM이 되면 awaiting_setup. 새 플레이어 활성화 또는 운영자 종료 필요. 새 캐릭터를 엔진이 스스로 모집하지 않음 |
| 부분 공개 | 소유한 단서 일부의 본문에서 배제 정보를 가림. opaque clue ID. 부분 단서의 재공유/재응답으로 원문을 복원할 수 없음 |

위 설정은 games/create의 `rules`로 변경한다. 탐색률·lease·한도는 범위 검증한다.
오답 사망·시체·세밀한 협상·특수 스킬은 새 회의안건의 P2이며 v1에서 제공하지 않는다.
기존 1인용/내장 데모 규칙과 v1 규칙이 같다고 가정하지 않는다.

## ID와 자격

| 이름 | 의미 / 재사용 |
| --- | --- |
| agent_id | 풀의 영속 정체성. 등록 후 중복 등록/다른 정체성으로 재사용 금지 |
| pool_token | 해당 agent의 일반화된 장기 상태와 **본인에게 배치된 현재 역할의 자격**만 조회. 그 자체로 게임 관찰을 읽는 자격은 아님 |
| game_id | 사건 단위. 새 게임은 새로운 ID. seed는 운영자 재현 설정이며 에이전트 관찰에 제공하지 않음 |
| assignment_id | 게임별 역할 배치. GM 승격은 같은 agent_id의 새 GM assignment |
| character_id | 회차별 플레이어 캐릭터. 승격/장애 시 정보 보존된 NPC로 남음 |
| controller_id / term / controller_token | 현재 GM agent_id / 증가하는 임기 / 교체된 capability. 매 명령과 cached 재전송 전에 검증 |
| token | 활성 플레이어의 회차별 capability. NPC/승격 시 즉시 폐기 |
| task_id / generation | GM이 만든 task / 소유권 변경 세대. phase_epoch와 다른 역할 |
| action_id | 능동 행동 멱등 ID. 같은 내용 재전송은 최신 저장 결과 반환; 내용 변경은 conflict |
| question_id / conversation_id | 등록된 요청. reply_to와 response_id로 최종 응답 연결 |
| event_id / sequence | 게임별 순서 있는 공식 이벤트. source_event_id로 공식 경험 연결 |

후임은 엔진의 GM 관찰·이벤트·map으로 맥락을 재구성한다. 엔진은 기존 플레이어 token을
폐기하고 GM 조회에서 개인 목표·개인 본문을 제거한다. **B/C 런타임도 승격 전 개인
프롬프트/신념/캐시를 GM 세션에 복사하지 않아야 한다**. 서버는 클라이언트 메모리를 삭제할 수 없다.

## HTTP 경로와 권한

모든 표의 경로는 `/integration/v1/` 뒤에 붙인다. POST JSON + `X-GM-Client: local`.
catalog는 공개 GET/POST도 지원한다. `schema_version`은 생략 또는 `engine-integration-v1`.

공개 GET `/integration/v1/health`는 API 활성화·watchdog 실행·최근 엔진 감시 오류를 표시한다.

| 경로 | 권한 | 주요 입력 / 결과 |
| --- | --- | --- |
| catalog | 공개 | 후보·장소·직업·trait·goal·규칙·버전. 숨겨진 단서 목록 없음 |
| pool/register | operator_token | agent_id, model_version → 영속 ID와 pool_token |
| pool/list | 운영자 | 가용성·배치 game_id·역할별 역량·버전. token/전략 본문 없음 |
| pool/credentials | 운영자 | agent_id → 자기 런타임에 재전달할 pool_token |
| pool/status | 운영자 | 할당되지 않은 agent의 available/dormant/fault 변경 |
| pool/learning/get | agent_id + pool_token | 자기 learning과 공식 경험. 타인의 기억 조회 불가 |
| pool/learning/update | 동일 | expected_version, experience_event_ids, strategy_state. CAS·본인 피드백·중복 소비 검사 |
| pool/session | agent_id + pool_token | 본인의 현재 game/role/context_key와 새 역할 credential. 타인의 자격/봉인된 개인 상태 불가 |
| games/create | 운영자 | game_id, seed, candidate_ids?, rules? → 풀에서 GM 선발. 초기 배분은 하지 않음 |
| games/list | 운영자 | 사건 상태·현재 GM ID/term 요약 |
| games/credentials | 운영자 | game_id → 현재 GM capability와 활성 플레이어 자격. 공개 이벤트에 포함하지 않음 |
| games/observe | 운영자 | GM이 없어도 map·상태·역할 이력·개입 선택지를 조회 |
| games/operator | 운영자 | PAUSE, RESUME, APPOINT_GM, FAIL_GM, FAIL_PLAYER, FINISH |
| gm/observe | 현재 GM | 공유·감독 ID·map·대안·pool 역량·질문 상태·기본 지표 |
| gm/heartbeat | 현재 GM | lease 갱신. 긴 추론 중에도 별도 heartbeat 필요 |
| gm/command | 현재 GM | PLAN / ADVANCE / FINISH와 command_id |
| agent/observe | 활성 플레이어 | 자기 profile/정보, 공유 정보, 공식 candidates/board, 허용 행동·할당 task |
| agent/heartbeat | 활성 플레이어 | lease 갱신. 응답/모델 대기 동안 독립적으로 갱신 |
| agent/action | 활성 플레이어 | request → completed 또는 pending 공식 결과 |
| agent/result | 해당 플레이어 | action_id → 최신 저장 결과; 타인 receipt 불가 |
| agent/share | 소유자 | message_id, information_ids, disclosure=full/partial → 선택 공개, 소비 없음 |
| questions/inbox | 해당 수신자 | 자신의 pending 요청·topic·발신자·기한 |
| questions/respond | 해당 수신자 | question_id, response_id, offer_ids, refused, disclosure → 최종 상태 |
| events | 현재 GM / 플레이어 | after → 허용 이벤트와 전체 event_sequence cursor |
| documents/search | 플레이어 | query → 본인 획득/공유 사건 자료만 검색; 조회 이벤트 기록 |

POST의 공통 `game_id`가 필요하다. GM은 controller 세 자격, 플레이어는 agent_id/token을 보낸다.
HTTP 401 unauthorized, 404 not_found, 400 invalid_input, 409 conflict/stale_controller/
expired/pending/not_running/unsupported_version을 `{schema_version, code, error}`로 반환한다.
외부 API의 유효 JSON 범위는 기존 서버와 같은 18,000 bytes 이하다.

## PLAN / agent map

PLAN은 GM이 만든 선택을 검증·저장할 뿐 엔진이 대안을 평가하지 않는다.

```json
{
  "operation": "PLAN", "command_id": "plan-unique-1", "map_version": 0,
  "activate": [
    {"agent_id": "common-2", "job_id": 0, "personality": "cooperative", "personal_goal": "protect_secret"},
    {"agent_id": "common-3", "job_id": 1, "personality": "cautious", "personal_goal": "prove_innocence"}
  ],
  "tasks": [{"task_id": "inspect-lounge", "agent_id": "common-2", "kind": "explore", "room": 0, "dependencies": []}],
  "alternatives": [{"id": "parallel", "summary": "병렬 조사"}, {"id": "verify", "summary": "상호 검증"}],
  "selected_alternative": "parallel", "reason": "GM이 선택한 근거", "evidence_ids": []
}
```

필수 capability/game_id는 위 예시에서 생략했다. GM 선발 결과가 common-2/3이면 다른 풀원을
활성화해야 한다. 동시 PLAN은 map_version 충돌로 거부한다. 기존 task 변경에는 현재 generation이
필요하고 완료 task 변경은 불가하다. 누락 의존성·사이클·비활성 owner는 전체 롤백한다.
처음 활성화는 setup, 추가 활성화는 fresh movement에서만 허용한다. 직업/성향은 활성화 때 고정한다.
각 task는 **한 번의 성공한 능동 행동**이며 조사 장소 전체 단서를 모두 모았다는 뜻이 아니다.

agent_map은 version + Agent/Character/Task/Location/Clue 노드 + assignedTo/dependsOn/locatedAt/knows 관계다.
그래프 ID는 agent:/task: 등 type namespace를 쓰고 entity_id는 공식 원래 ID를 보존한다.
승격자의 NPC Character와 GM Agent를 별도 노드로 두며 GM에게 이전 캐릭터의 knows/controls를 옮기지 않는다.
version은 계획/소유권의 map_version이며 state_sequence는 위치·증거 등 공식 사실의 event cursor다.
시각화는 두 값을 함께 사용한다. 별도 관찰에서 나온 그래프의 동적 위치가 같은 계획 버전에서 바뀔 수 있다.
knows는 단서 ID를 인지/소유한다는 뜻이며 공개 허가가 아니다. 미발견 단서는 노드에 넣지 않는다.
대안·reason은 GM 감독 기록에만 보관하며 개인 내용을 공유 게시판에 자동 게시하지 않는다.

ADVANCE는 현재 phase_epoch, 전원 spent, pending 질문 0을 검사한다. 현재 snapshot으로 재계획한다.
FINISH는 엔진이 판정한 resolution=solved에서만 성공 종료하며 운영자는 별도로 중단할 수 있다.

## 직접 행동과 질문

```json
{
  "game_id": "case-1", "agent_id": "common-2", "action_id": "action-unique-1",
  "cycle_id": 1, "phase": "interaction", "phase_epoch": 2,
  "action": "question", "payload": {"target_id": "common-3", "approach": "compassion", "topic": "알리바이 자료"}
}
```

`agent/action`의 request에 넣는다. task를 참조할 때는 task_id와 **관찰 당시 generation**을 넣는다.
task 없이도 합법 행동을 직접 할 수 있어 GM이 모든 행동을 순차 승인하지 않는다.
move.payload.room, explore.payload.approach, accuse.payload.{suspect,weapon,place},
question.payload.{target_id,approach,topic}, stay/prepare/pass의 빈 payload를 지원한다.
question 대상은 같은 방의 다른 캐릭터다. 정상 플레이어이면 수신함에 기록하고 엔진은 응답을
기다리며 API 요청은 즉시 pending으로 반환한다. 알려지지 않은 단서도 topic으로 요청할 수 있다.

정상 응답: `(offer_ids, refused)`는 B가 결정한다. 엔진이 소유·활성 역할·기한을 재검증하고
동의한 후보 중 공식 판정으로 최대 한 개를 질문자에게만 전달한다. 거절 또는 판정 실패는
단서를 주지 않는다. 모든 terminal 결과는 acquired_information, held_information_ids,
execution_status, consumed, outcome, roll, task_id, action_id, 단계 문맥을 가진다.
응답 상태 answered와 행동 결과 succeeded는 다르다. 상대가 응답했어도 주사위 실패면 failed,
새 정보가 없으면 no_new_information이다. 정보 전달 성공을 응답률과 혼동하지 않는다.
question 재전송은 새 질문을 만들지 않고 완료 이후에는 최신 terminal 결과를 반환한다.

NPC는 엔진의 수동 정책으로 소유 증거를 제공할 수 있다. 정상 플레이어의 응답을 엔진이
성향 규칙으로 대신하지 않는다. timeout/fault/종료/task 재배정 시 기존 pending은
timed_out/cancelled/stale_task로 수렴하고 지연 응답은 거부한다. 응답 비용은 0이다.
PAUSE는 새 행동을 차단하고 watchdog를 정지한다. RESUME은 역할 lease와 질문 기한을 복원한다.

## 공개 범위 / 기억 / 피드백

- 공식 candidate ID는 정수; clue ID는 opaque 문자열; character/task/question ID와 혼용 금지.
- 공식 정보는 `{id, category, excluded_id, text}`. place는 엔진 명칭, B legacy projection의 kind는 location.
- 부분 단서는 `{id, category, redacted:true, text}`이며 excluded_id/excludes가 없다. 배제 근거로 쓰지 않는다.
- GM 기본 snapshot/events는 개인 목표·개인 증거 본문·정답·credential을 제거한다.
- player 이벤트는 자기/공개만. sequence에 간격이 있을 수 있어 event_sequence까지 cursor를 이동한다.
- 게임별 비밀은 games에, 영속 registry는 agents에, 일반화된 전략/역량은 agent_learning에,
  엔진 발행 피드백은 experience_events에 저장한다. 역할 배치 이력은 해당 game의 assignments에 보존한다.
- strategy_state는 `{"tactics":[catalog의 코드],"weights":{"team":1,"information":2}}`처럼
  일반화된 코드와 유한 수치만 저장한다. raw 단서·정답·개인 목표·자유문장은 받지 않는다.
  이것은 v1의 제한된 저장 계약이며 B/C의 전략 학습 알고리즘이 아니다.
- competencies는 엔진의 공식 성공/실패로만 갱신한다. 플레이어는 행동 결과, GM은 종료/장애
  episode 결과의 기본 proxy다. 자기보고 점수는 선발에 반영하지 않는다. GM 공정성/장기 성능을
  이 proxy 하나로 입증할 수 없으므로 외부 평가 설계가 별도로 필요하다.
- learning/update는 본인 미사용 experience_event_ids와 expected_version을 검사한다. 전략을
  실제 다음 판단에 반영하는 것은 B/C 런타임의 책임이다. 점수 축적만으로 학습을 주장하지 않는다.

## B/C 어댑터를 연결할 때

`EngineClient.action_request()`는 저장한 관찰에서 요청과 generation을 만든다.
`reasoning_inputs()`의 기본값은 최신 B **d06c650**의 singular 후보 키와 dict profile을 투영한다.
`legacy=True`만 이전 plural 후보/kind/excludes를 투영한다. 최신 B는 BeliefState.apply를 제공하며
deploy 폴더의 flat import는 실행된다. 루트의 `import deploy.agent`는 여전히 schema 모듈을 찾지 못한다.
엔진은 B의 import나 신념 구현을 monkey patch하지 않는다.

지속형 워커는 자기 pool_token으로 `pool/session`을 폴링해 활성화/승격을 받는다.
`EngineClient.session()` → `role_client(session)`으로 운영자 token 없이 자기 역할 자격만 가져온다.
context_key가 바뀌면 플레이어/GM 단기 맥락을 새로 만들고 지정된 관찰에서 복원해야 한다.
풀의 장기 전략 조회 client와 게임 role client를 따로 유지한다. role_client에는 pool/operator token이 없다.

```powershell
python -B tests/probe_team_player.py
```

위 선택 검증은 teams/의 **실제 B LLMAgent 코드**에 모델 응답 대역을 주어 sync/decide/응답/신념 갱신을
확인한다. native flat entrypoint를 그대로 쓰며 팀 코드나 키를 수정하지 않는다.
유료 LLM·B PlayerAdapter·C Coordinator 전체 통합 검증은 아니다.

C의 provisional EnginePort와 의미는 대응하지만 현재 C 코드는 **그대로 꽂을 수 없다**:

| C provisional 접점 | v1 연결 / 필요한 변경 |
| --- | --- |
| create_game + gm-primary 고정 | 운영자 games/create가 선발한 실제 controller_id/term으로 Coordinator 구성 |
| catalog / gm_snapshot / agent_snapshot | catalog / gm/observe / agent/observe; 공식 ID와 활성 역할 자격 사용 |
| events_after | events; 기본 GM는 획득 ID만 받으므로 acquired_information 본문 가정을 제거 |
| submit_action(ActionRequest) | agent/action; C도 관찰 task generation을 유지하거나 task 미참조 요청 사용 |
| advance_phase | gm/command ADVANCE; phase_epoch와 현재 GM capability 필수 |
| take_over | 엔진 watchdog / 운영자 명령. 일반 GM이 임의로 승계시키지 않음 |
| convert_fault | 엔진 watchdog가 확정 또는 운영자 FAIL_PLAYER. GM는 해제된 task의 새 owner를 PLAN으로 결정 |
| finish | GM FINISH는 solved 확인, 운영자 FINISH는 중단 |
| answer_question | questions/inbox → B respond_to_question → questions/respond → requester agent/result |

C의 provisional은 personality를 dict로 기대하는 곳도 있어 string trait에 대한 투영이 필요하다.
최신 B d06c650의 decide는 `(action, payload)`이고 question은 이미 target_id를 쓴다.
C f00bc05의 ReasoningPlayerAdapter는 이전 3개 반환값과 target→target_id 변환을 기대하므로 수정해야 한다.
최신 B는 부분 단서의 ID를 처음 처리하면 같은 ID의 완전 공개를 다시 belief에 적용하지 않을 수 있다.
부분→전체 upgrade를 재반영하는 것은 B의 신념 처리 과제이며 엔진은 가려진/완전 정보 구분을 제공한다.
corpse/pass 소비·단계 허용 행동은 catalog에서 읽는다. B/C에서 제공되는 role-switch hook에 새
GM credential을 넘기되 operator_token/pool_token/타인 player token을 GM 프롬프트에 넣지 않는다.

## 연결 준비 합격 기준 / 제외 범위

```powershell
python -B -m unittest discover -v
python -B tests/run_external.py
python -B tests/run_browser.py
git diff --check
git check-ignore teams/GM_Agent_Society/README.md teams/Agent_Reasoning/deploy/agent.py
```

test_integration은 pool/CAS/가시성/질문/단계/권한/동시성/재시작/CLI 경계가 중심이다.
run_external은 임시 DB와 실제 HTTP로 **명시적 스크립트 대역**을 실행한다. 서버/클라이언트
접점의 증거이지 실제 B/C 공동 실행·LLM 판단·전략 향상·H1~H3의 증거가 아니다.
기존 브라우저 E2E는 legacy UI 회귀이며 외부 API UI 검증이 아니다.

v1 제외: 실제 B/C 코드를 수정/실행해 통합하기, 외부 프로세스 강제 종료, 공통 런타임의
학습/승격 구현, 운영 배포 인증·다른 DB 사이 합의, 오답 사망/시체, LLM 호출/비용 수집,
대규모 비교 실험 및 GM 충돌 해결 알고리즘. 엔진은 GM 대안과 선택을 기록하되 대신 추론하지 않는다.
