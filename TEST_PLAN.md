# GitHub 반영 전 테스트 계획

대상: `internal_docs` 요구에 따른 멀티에이전트 기능과 기존 1인용 게임의 회귀 여부.

## 통과 기준

| 항목 | 검증 방법 | 통과 기준 |
| --- | --- | --- |
| 기존 1인용 게임 | `test_game.py` | 단서 25개·정답 보존, 판정/비용, 대기 판정 우회 방지, 저장 복원, API 어댑터 테스트 전부 통과 |
| 가변 인원·재현 | `test_multi.py` | 1/3/12명 생성, 동일 seed의 사건·태그 일치, 잘못된 인원 거부 |
| 정보·권한 경계 | `test_multi.py` | GM/다른 플레이어/모델 요청에 숨겨진 정답·타인의 개인 상태·인증 토큰 미노출, 잘못된 자격 거부 |
| 행동 계약 | `test_multi.py` | 동일 행동 ID 재실행 방지, 변경 요청 거부, 실패한 담당자의 지연 결과 거부, 부분/전체 공유·비공개 메시지 범위 유지 |
| 장애 복구 | `test_multi.py` | 실행 중 3→2→1명 감소 시 기존 공유 증거 보존, 미완료 작업 재배정, 최종 해결; 0명일 때 사람 개입 대기 |
| 비동기 진행 | `test_multi.py` | 한 플레이어가 응답 대기 중이어도 다른 플레이어가 두 번 이상 행동 가능 |
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

## 실행 결과 (2026-10-12)

- `python -m unittest discover -v`: 24개 모두 통과.
- `python tests/run_browser.py`: 화면 이동, 사건 생성, 두 명 장애, 작업 재배정, 한 명으로 해결, 개인 조회, 안전한 공개 내보내기, 새로고침 복원, 모바일 레이아웃 및 JS 오류 검사 통과.
- 실제 유료 API 호출은 미실행이며, 이번 반영의 필수 통과 범위에서 제외했습니다.
