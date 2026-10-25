# 경쟁형 엔진 · 기획 v1.3 연결 계약

작성: 2026-10-25. A의 기존 `/integration/v1/` 전송 계층을 유지하고, 기본 규칙을
`mode=competitive`, `ruleset=competitive-v1.3`으로 확장했다. 기존 저장 게임은 그대로 읽으며
협력형 테스트/클라이언트는 생성·카탈로그 POST에 `mode=legacy`를 명시한다.

## 기준과 책임

- 로컬 `internal_docs/`의 HTML 수정안·남은 작업 지도, 기획서 v1.1, 서브 미션·간접 단서 설계서를 확인했다.
- 최종 규칙은 C `gm-engine-v1`의 **GAME_SPEC_v1.3.md / GM_ENGINE_REQUEST_20261019.md**가 우선한다.
  HTML보다 후속 확정된 고정 GM·중간 참가 금지·경합 자원/공정성 감시 폐지를 적용했다.
- 확인한 저장소: A main b477cb0, B main f916097, C gm-engine-v1 c882496. `teams/`는 Git 제외다.
  `teams/gm_coordinator`는 독립 저장소가 아니라 기존 테스트 임시 폴더라 업데이트 대상으로 취급하지 않았다.
- A는 공식 상태·판정·권한·진실성·피드백을 집행한다. B의 목표 기반 판단과 C의 Role 선택을 대신하지 않는다.
  캐릭터·미션 공개 데이터는 C의 `v12_data.json`을 provenance와 함께 가져왔다(CC BY-SA 4.0).

## 요구사항 추적

| 기획 | 구현 |
| --- | --- |
| A1 / R2 | 캐릭터 12명 공통 시트, 스테이터스 합 11, 직업 스킬·출발 칸·MBTI, 플레이어/NPC 동일 데이터 |
| A2 | 확실한 단서 25개 + 자정 위치/동행/흉기 위치의 간접 단서, seed별 타임라인 검증, 장소 단서 복제 보존 |
| A3 | 이동→탐문·탐색→추리, 단계당 행동 1회, 응답 무료, NPC 이동, 120사이클 마감 |
| A4~A6 | 2d6/준비 +1d, 기준선 8−스탯, 스킬·관계 보정, 네 접근·위험·0~3성공 강제 공개량 |
| A7 | 12노드 132개 방향성 관계, 50 시작/0~100 제한/E·I 배율, 본인 outgoing만 조회 |
| A8 | 보유 ID/분류 제안, 가설/분류 요청, 수락·거절·역제안, 양측 개별 이전, 불이행·평판 |
| A9 / R7~R8 | 추리 동시 판정, 출력 토큰 순위/동률 공동 승리, 차등 점수, 엔진 작성 공지 참조 게시 |
| A10 | 오답 사망·시체, 시체 조사, 사이클당 단서 1개 휘발, 타인의 이미 얻은 정보 보존 |
| A11 / R5 | 질문 쌍만 공개, 당사자 정보/GM 감독/연구 로그 분리, 타인 MBTI·목표·원문 미노출 |
| A12 / R1·R6·R9 | 17명 고정 풀, GM 유임/영구 승격, 참가 이력·점수, GM 성과 제출, 예비 후임과 최후 운영자 종료 |
| A13 / R3 | 58종 난이도 조정, 난이도별 3후보, 직전 미션 제외, 3개 선택/대상 검증/모든 달성 판정 |
| R4·R10 | 경쟁형 action tasks 금지, Role 시작 시 일괄 3~12명, 중간 활성화·NPC 재배정 금지 |
| A1 런처 / A2 화면 | `launcher.py`, `/competition`의 관전·운영·방향성 관계망·일시 중지/재개 |

플레이어는 0단서로 시작하며, NPC만 출발 칸의 단서를 안다. 범인은 플레이어 캐릭터일 수도 있으나
본인에게 알리지 않는다. 사례의 정답·타임라인을 GM/플레이어 프롬프트에 제공하지 않는다.
기획서에서 폐지한 행동 작업 planner·자발적 공유·GM 공정성 감시·경합 슬롯은 새 모드에 넣지 않았다.

## 풀과 세션

운영자 `pool/bootstrap {pool_id?, model_version?}`가 고정 GM 1명(초기 MBTI 없음)과
MBTI 16종 플레이어 16명을 한 번 생성한다. 재호출은 같은 풀을 반환한다. 모델은 풀 단위로 고정된다.
영속 플레이어 ID는 **MBTI를 인코딩하지 않는 opaque ID**다. 대화 상대 ID로 타인 MBTI를 알 수 없다.

운영자 `games/create {game_id?, pool_id?, seed?, rules?, provenance?}`는 현재 GM을 이어 쓴다.
GM 역량/탐색률로 매 게임 재선발하지 않는다. GM은 `pool/session`에서 새 Role credential을 받고
본인의 새 `context_key`마다 엔진 관찰만으로 맥락을 다시 만든다. 이전 플레이어 기억은 복사하지 않는다.

진행 중 GM 장애는 유효 lease의 활동 플레이어 ID 정렬순 → 가용 예비 풀원 순으로 승계한다.
승격된 플레이어의 캐릭터는 시트·MBTI·정보를 유지한 goal 없는 NPC가 되고 예전 자격은 폐기된다.
후임은 이후 게임에서도 GM으로 유임하며 플레이어로 돌아가지 않는다. 0명은 `no_players` 종료를
준비하고 GM이 FINISH한다. 후보가 전혀 없을 때만 `waiting_for_human`에서 운영자가 종료한다.
기술 장애 풀원은 자동 복구됐다고 가정하지 않는다. 게임 종료 후 운영자가 `pool/status`로 가용성을 복원할 수 있다.

## Role 배정: R3·R5의 2단계 계약

GM은 `gm/observe`에서 풀의 fixed_mbti·history·캐릭터 시트를 받는다. 목표·원문은 받지 않는다.
직업 역량·커버리지와 참가자/캐릭터 선택은 **C의 정책**이다. 엔진은 참가 기록을 제공한다.

1. `gm/mission-candidates {activate:[{agent_id, character_id}, ...]}`
   → `mission_candidates[agent_id][하|중|상]`, 배정 후의 MBTI/controller 미리보기, 지정 가능한 간접 ID.
   캐릭터에 따라 난이도가 달라지므로 Role 후보를 먼저 제시한다. 불필요한 16×12 조합을 모델에 넣지 않는다.
2. `gm/command PLAN {command_id, map_version, activate:[{agent_id, character_id, sub_missions:[{mission_id,tier,targets,params}]}], alternatives, selected_alternative, reason}`
   → 역할·MBTI·미션 난이도·대상·후보·중복을 모두 검증하고 원자적으로 저장한다.

`tasks`는 거부한다. 대안은 2개 이상이어야 한다. goal의 미션은 상·중·하 각 1개이고 원문/정답에
의존해 후보를 만들지 않는다. M51의 N은 허용 수량 중 실제 난이도가 그 칸과 맞아야 한다.
같은 수량형 미션이 두 난이도에 중복 배정되지 않도록 후보 슬롯을 한 곳으로 제한한다.
`targets.target_ei`는 GM 분석용 메타데이터이며 플레이어·종료 공개에는 넣지 않는다.

`gm/observe.agent_map`은 Agent/Role/Character/Location 노드와 assignedTo/represents/locatedAt 관계다.
NPC는 에이전트로 세지 않는다. 관계성 weight는 GM map에 공개하지 않는다.

## 직접 행동·응답

HTTP POST `/integration/v1/...`, `X-GM-Client: local`, 역할별 capability는 기존 방식과 같다.
`engine_client.EngineClient.action_request()`로 관찰 당시 cycle/phase/epoch를 고정한다.

| 단계 | action / payload |
| --- | --- |
| 이동 | move `{room}` / stay `{}` |
| 탐문·탐색 | explore `{}` 또는 시체 `{target_id}` / prepare `{}` / pass `{}` |
| 질문 계열 | question `{target_id: 캐릭터정수, approach: question|interrogate|threaten|negotiate, hypothesis:{suspect,weapon,place}}` |
| 추리 | accuse `{suspect,weapon,place,output_tokens,elapsed_seconds?}` / pass `{}` |

질문 등록은 즉시 pending을 반환한다. 질문자는 같은 action_id를 재전송하거나 `agent/result`로
완료 결과를 받는다. 수신자는 `questions/inbox`의 **현재 `response_options`와 최신 관찰**을 사용한다.
답은 `questions/respond {question_id,response_id,statements}`다. offer_ids/refused의 구형 형식이 아니다.

- definite: `{kind:"definite",clue_id}`
- ambiguous: `{kind:"ambiguous",category,candidate}` — 엔진이 보유 사실과 비교하고 문장을 생성한다.
- indirect: `{kind:"indirect",clue_id,text}` — 자기에게 허용된 narrative_options 중 하나.

공개량은 0/1/2/3성공별로 검사한다. 거짓·중복·의무 미달은 전체 롤백한다. 의무 형태를 만들 수
없는 특수 경우는 실제 보유 정보로 가능한 범위만 강제하며, 거짓 정보를 만들어 채우지 않는다.
응답 기한이 지나면 엔진의 명시적 강제 정책이 같은 범위의 진실한 응답을 확정한다.
질문자는 획득/정보 없음만 받고, 제3자는 질문 쌍만 받는다. NPC도 같은 판정을 거친다.

## 간접 단서와 진실성

타임라인은 자정의 사람·흉기 위치와 동행을 생성한다. 25개 확실 단서가 모든 오답을 배제하며,
간접 사실도 그 정답과 모순되지 않는지 생성 시 검사한다. 장소의 단서는 각 플레이어가 따로 얻는다.

엔진은 수신 전 정보와 새 간접 단서 원문을 조합했을 때 새 배제가 생기는지 **비공개 연구 기록**에 남긴다.
이 counterfactual 원문·배제 결과는 질문자나 GM에게 주지 않는다. 플레이어에게 받지 않은 사실을
자동 추론해 제공하지 않으며, 실제 추론은 에이전트가 한다.

자유 한국어의 모순을 완전히 판별한다고 주장하지 않는다. LLM 검사 대신 **폐쇄형 진실 보존 문법**을
선택했다(기획서 E의 엔진 재량). 생략·직업/이름 지칭·불확실성 표현의 검증 가능한 표현 목록을 제공하고
다른 자유 문장은 fail-closed로 거부한다. 플레이어는 목록에서 생략 내용·표현·단서를 고른다.
받은 것은 narrated ID/서술문이며 원문 필드는 없다. 기존 narrated가 나중에 full로 갱신되는 것도 지원한다.
이 한계와 데이터 편향은 자유 생성 서술의 연구 결과와 구별해야 한다.

## 관계성 메모에 대한 판단

방향성 가중 인접 그래프가 적절하다. 12명이라 별도 그래프 패키지 없이 132개 edge를 저장한다.
다음 게임에는 50으로 초기화한다. 기획서 A7과 충돌하는 '기존 안면 초기값'은 기본 게임에 넣지 않았다.

- `relationships/graph`: 본인 outgoing 11개만. 관계성 미션은 반대 방향을 검사하고 진행 수치를 숨긴다.
- 운영자 `games/observe`: 전체 방향 그래프. `/competition`은 선택한 source의 11개 edge를 그린다.
- 질문/교섭 기준선, F NPC의 확실 응답 확률, E/I 변화량, 교섭 불이행에 실제로 연결된다.
- 관계성은 회차별, 교섭 평판/참가 이력은 영속 풀이다. 둘을 혼동하지 않는다.
- `python tests/relationship_experiment.py`: 동일 시트·동일 난수 1,024회씩으로 값 20/50/80을 비교하고
  `relationship_effects=False` 대조군에서는 판정 차이가 사라지는지 검사한다. **엔진 규칙의 효과이지
  LLM 상호소통/학습 효과를 입증한 실험은 아니다.**

## 비구속 교섭과 피드백

탐문·탐색 단계의 부가 메시지로 `trades/propose`를 1회 보낼 수 있다(능동 행동을 추가 소비하지 않음).
대상은 같은 칸이며 offer_ids 1개 또는 offer_category, 요구는 requested_category 또는 requested_hypothesis다.
`trades/respond`는 accept/refuse/counter, `trades/transfer`는 각자 독립적으로 이전한다.
분류만 약속했다면 transfer에 information_id로 실제 단서를 고른다. message_id 재전송은 중복 실행하지 않는다.
동시 이전·상호 이행은 보장하지 않는다. NPC J/P 수락/이행 기본값은 코드와 catalog 문서에 고정된다.
기한/종료 시 이행·불이행을 확정하고 관계성과 영속 평판/본인의 peer_reputation에 기록한다.

게임 종료 시 history는 game_id·character_id·job_id·final_score·participation_index와 미션 종류/달성을 유지한다.
raw 단서·관계·지정 단서 ID는 장기 history에 복사하지 않는다. `pool/learning/get`은 본인 이력·일반화 전략·
공식 경험·상대와의 본인 거래 평판·GM reports를 준다. `pool/feedback {game_id}`는 참가했던 종료 게임의
공개 미션/순위와 본인 교섭 결과를 준다. 새 게임에 배치되면 예전 Role token으로 옛 비밀을 읽는 것은 차단한다.

추리 출력 토큰은 신뢰하는 실행 런타임이 게이트웨이 usage를 제출하는 값이다. 엔진이 외부 API 청구량을
독립 검증한 것은 아니다. 대역은 0을 명시한다. 전체 풀 모델을 같게 설정하고 실제 시간을 순위에 쓰지 않는다.

## 공지·종료·평가·저장

GM `ADVANCE`는 전원 완료/응답 종료를 검사한다. 추리 단계 끝에 도전을 모두 판정하고 동률은 같은 순위다.
오답은 시체, 정답자는 모두 승자다. 순위 점수 100/70/50/30, 생존 패배 0, 사망 −20에 미션 10/20/30을 더한다.
엔진 resolution은 winner/all_dead/deadline/no_players. GM은 공식 ref로 ANNOUNCE한 다음 FINISH한다.
ANNOUNCE 본문을 GM이 바꿀 수 없다. 마감 경고는 30/10사이클 전이다.

종료 뒤 GM `REPORT {metrics:{completion,balance,exploration,record},evidence:[event_id]}`를 받는다.
balance=null은 가중치를 제외해 재정규화한다. 기술 장애 플래그를 엔진이 붙여 score≥0.7 성공을 기록한다.
낮은 점수로 GM을 바꾸지 않는다. 운영자 종료/0명 종료를 정당한 진행 완결이라고 허위 보고할 수 없다.

핫 상태·완료 기록을 분리했다. `competitive_events`와 `competitive_records`에 완료 이벤트/행동/명령/응답을
같은 SQLite 트랜잭션으로 저장한다. 재전송 전 현재 역할·임기를 검사하며 재시작 뒤에도 같은 결과를 돌려준다.
완료 기록을 매 API마다 게임 JSON에 중첩 복사하지 않는다. 두 watchdog도 BEGIN IMMEDIATE로 승계를 직렬화한다.

## 실행과 검증 범위

```powershell
python -B -m unittest discover -q
python -B tests/run_browser.py
python -B tests/run_competition_browser.py
python -B tests/run_external.py
python -B tests/probe_team_player.py
python -B tests/relationship_experiment.py
python -B launcher.py --seed 7 --progress
python -B launcher.py --scenario gm-fault --seed 7 --progress
python -B launcher.py --scenario human --seed 7 --progress
```

`launcher.py`는 실제 C의 Role/미션/성과 순수 함수를 호출한다. 플레이어는 C의 명시적 스크립트 대역에
최신 관찰·full upgrade·허용 응답 목록을 맞추는 **공개된 legality adapter**를 붙였다.
필터링한 대역 제안 수를 출력한다. GM 선택을 엔진이 대신하거나 팀 저장소 원본을 monkey patch하지 않는다.
`human` 이름의 시나리오는 최신 규칙에서 **예비 GM이 0명 사건을 종료**하는 사례다.
예비까지 전부 소진된 운영자 최후 종료는 별도 자동 테스트로 검사한다.

B f916097의 기존 LLMAgent는 legacy 도구·응답에 대한 기본 스텁 검사만 통과했다.
실제 B의 v1.3 스테이터스/성공 단계 응답/출력 토큰/역할 전환 런타임은 B에서 연결해야 한다.
이 작업은 B의 판단 코드를 몰래 대체해 전체 LLM 공동 실행이 된 것으로 보고하지 않는다.
비용이 발생하는 호출은 하지 않았다. 학습 향상·LLM 신빙성·대규모 통계적 우위도 미검증이다.
