# 다음 모델 구현 인계 — 2026-10-04

> 2026-10-04 재개 기록: 아래 중단 상태에서 구현을 이어받아 클라이언트 문법 오류와 동기화 경계를 복구했다. Python 회귀 159개와 Anki 번들 Qt 패널·도크·설정 스모크가 통과했다. 12개 소스와 일치하는 `study_companion.ankiaddon`을 다시 만들었고 SHA-256은 `416319c72ddaf16c7e689d26ed1d3a6596737fce9bf2d64ec8f103bbe3bd8ac2`이다. 설치본은 백업 후 교체했으며 실행 중인 Anki에는 다음 재시작부터 로드된다. 서버 90일 보관 migration은 운영 PostgreSQL의 rollback 회귀 검증 후 적용했다. 스키마·권한 검증과 첫 제한 배치(`0일 / 0건`)도 통과했으며 자동 예약만 비활성 상태다. 아래 내용은 판단 근거와 당시 상태를 보존한 인계 기록이다.

## 사용자 최신 지시

“지금까지 한것들 기록해두고 어떻게 할지 설계만 해 구현은 다른 모델로 할거야.”

이 지시에 따라 구현 에이전트 둘을 중단했다. 이후 작업은 상태 확인과 이 문서/TODO 기록뿐이다. 다음 모델은 사용자가 구현 재개를 요청할 때 아래 작업을 진행한다.

## 반드시 먼저 읽을 현재 상태

**작업 중 소스는 배포 가능한 상태가 아니다.** 중단 당시 `study_companion/addon.py`의 `fetch_members` 조건에 `or`가 빠져 문법 오류가 있다(확인 시 694~697행). 이를 포함한 새 수정은 미검증이다. 사용자 중단 지시에 따라 고치거나 되돌리지 않았다.

- Git HEAD: `2c2483f` (`Add friend activity timeline and personal weekly rhythm`). 원격: `https://github.com/EternalOlive/anki-study-companion.git`.
- 이전 턴의 성능 개선은 아직 미커밋/미푸시 상태다. 이번 미완성 수정도 같은 파일에 겹쳐 있다. `git checkout`/reset으로 전부 되돌리면 이전 완료분까지 사라지므로 금지한다.
- 배포 파일: `study_companion.ankiaddon`, 51,933 bytes, 12개 파일. SHA-256: `6caa1cd5f2a7c878ea953e25dfe4659084bb429bc5cf96c5cb33c6e1df0d0a3b`.
- 배포 파일 내부 `addon.py`는 구문 검사 통과. 현재 소스와 다른 파일은 `addon.py`, `reviews.py` 두 개다. 현재 소스로 패키지를 덮어쓰지 말 것.
- 앞선 턴에 위 패키지와 일치하던 두 파일을 `C:\Users\yoon\AppData\Roaming\Anki2\addons21\study_companion`에 설치하고 해시 일치를 확인했다. 이번 미완성 소스는 설치하지 않았다. 실제 Anki 재시작/로드 여부는 미확인이다.
- 이전 설치본 백업: `backups/before-performance-20261004/`. 이 백업은 성능 개선 **이전** 버전이다. 검증된 성능 개선 버전은 현재 `.ankiaddon` 안에 있다.
- 중단 후 확인된 변경: `TODO.md`, `.ankiaddon`, `study_companion/addon.py`, `study_companion/reviews.py`, `tests/test_offline_sync.py`, `tests/test_reviews.py`; 미추적 `RELEASE_CHECKLIST.md`. 이 문서도 새 파일이다.
- 새로운 서버 보관 migration/SQL 테스트는 아직 생성되지 않았다. 이번 작업에서 운영 DB 스키마/보관 작업/스케줄은 변경하지 않았다.

## 이미 완료한 기능과 검증

기존 기능 상세는 README와 TODO를 따른다. 오늘 답변 횟수/시간은 Anki revlog 기준이며 모바일 기록은 AnkiWeb을 통해 PC에 동기화된 후 반영된다. 익명 사용, 아이디·비밀번호 연결/복구, 비공개 방, 덱 이름 선택 공유, 친구 오늘 활동 시간띠, 개인 최근 7일 기록이 구현돼 있다.

이전 성능 개선에서 완료한 사항:

1. 기기 상태는 30초 전송 유지, 무거운 친구 조회는 90초 간격.
2. 공유 덱 값 변경 시 전송, 같은 덱 공부 중에는 서버 90초 TTL보다 짧은 60초 갱신. 빈 덱 상태는 반복 전송 생략.
3. 새 방에 어제·오늘 기록만 전송하되 이미 전송을 시도한 과거 날짜는 재시도.
4. 로컬 최근 3일 캐시 유지, 오래된 전송 확인 완료 캐시 정리. Anki 원본은 그대로 보존. `mark_route_days`로 관찰된 미전송 날짜 보호.
5. 당시 Python 테스트 150개 통과, 패키지 12개 파일 해시 대조 통과. 이는 **현재 미완성 소스**의 테스트 결과가 아니다.
6. 합성 180일×100답변, 전부 전송 확인 완료 상태: 2,841,971 → 32,459 bytes. 사용자 실제 데이터/성능 측정은 아니다.

서버 점검 당시 Free 요금제, DB 약 6%, 전송량 0.05/5GB, 한도 초과 없음. 정확한 기간/수치/한계는 TODO 참조. 유료 전환은 요청받지 않았으며 수행하지 않는다.

## 이번 중단 시점의 미완성 수정

`addon.py`:

- 친구 조회 캐시 키를 `(user_id, group_id, study_day)`로 구분하고 `force=True`, 계정/방/날짜 변경 때 즉시 조회하려는 수정.
- 패널을 다시 펼칠 때 강제 갱신하려는 수정.
- 덱 게시 캐시에 사용자 ID를 추가하려는 수정.
- 친구 조회만 실패했을 때 성공한 업로드 확인까지 버리지 않도록 예외 처리를 분리하려는 수정.
- 조건 연결 문법 오류가 있으며 캐시 키를 사용하는 테스트 더블, 401 재인증 처리도 다시 검증해야 한다.

`reviews.py`:

- 방·컬렉션별 고정 `since_day`를 도입하여 오프라인 기간 동안 공유 범위가 계속 뒤로 밀리지 않게 하려는 수정.
- `compact()`에서 날짜별 확인 상태가 없어도 해당 방의 전송 대상일 수 있으면 보존하려는 수정.
- 구버전 상태에 `since_day`가 없을 때의 이행 규칙은 아직 검증되지 않았다. 현재 시점을 무조건 대입하면 구버전 미전송 날짜를 건너뛸 수 있으므로 특히 점검할 것.

## 구현 설계와 작업 순서

### 1. 클라이언트 정확성부터 마무리

최소 수정으로 위 미완성 코드를 완성한다. 캐시 키는 사용자·방·날짜를 포함하고 세션 시작/수동 갱신/방 변경/재참가/자정/패널 펼침에 갱신한다. 동일 방에 재참가한 경우도 이전 상태를 쓰지 않도록 현재 route epoch 또는 명시적 캐시 무효화를 활용한다. 30초 heartbeat는 유지한다.

공유 시작일은 방 참여 시 확정해 지속 저장한다. 기존 상태는 현재 route와 보관 날짜를 근거로 보수적으로 이행한다. 판단이 불가능한 오래된 미전송 데이터는 보존한다. 새 방에는 최근 범위만 전송하면서 기존 방의 오프라인 데이터는 유실하지 않아야 한다.

회귀 검증: 답변 후 첫 동기화 전에 종료하고 3일 뒤 복귀, 빈 날짜 활성화, 미확인 취소 기록, 동시 전송 중 undo/redo, 같은 방 재참가, 같은 계정 새 토큰, 계정 전환, 시계 역행, TTL 경계. 친구 조회 실패와 인증 만료를 구분해 성공한 전송 확인과 재로그인 동작을 함께 보존한다.

### 2. 서버 상세 기록 보관 정책

제안: **최근 90일 상세 기록 유지, 오래된 날짜별 합계 보관**. 날짜는 방 기준 UTC+9다. 90일은 설계상 기본값이며 사용자에게 제안한 상태다.

데이터 구조는 기존 `anki_review_day_markers`에 `archived_at`을 추가하고, 비공개 `anki_review_day_archives`를 `(group_id, user_id, collection_id, study_day)`로 둔다. 합계는 답변 수와 밀리초 합계를 저장한다. FK를 기존 날짜 마커에 연결해 방 나가기의 cascade와 일치시킨다. 클라이언트의 원본/집계 테이블 직접 접근은 금지하고 기존 같은 방 RPC 권한을 유지한다.

정리 함수는 관리자/service role만 호출한다. 한 번에 처리하는 날짜 수를 제한하고, 최근 90일을 더 짧게 지울 수 없도록 인자를 검증한다. 날짜 마커 잠금 → 활성 이벤트 합계 저장 → 날짜 잠금 표시 → 상세/취소 행 정리를 같은 트랜잭션에서 수행한다. `sync_review_day`도 동일 잠금 규칙을 따라 정리와 업로드가 경쟁하지 않게 한다. timeout과 인덱스도 확인한다.

`get_group_device_stats`는 보관된 날짜에는 저장한 일별 합계를, 최근 날짜에는 상세 기록을 사용한다. 컬렉션별 합계를 사용자별로 합치며 중복 집계를 피한다. 오늘 활동 시간띠는 기존 오늘 상세 조회를 유지한다.

**보관 완료 날짜에는 늦은 업로드를 성공 처리하지 않는다.** 명시적인 `review day archived` 오류를 반환해 오래된 재전송/undo가 원본을 부활시키지 않게 한다. 아직 수집된 적 없는 과거 날짜를 허용할지, 갱신 후 일정 유예기간을 둘지 구현 전 일관된 정책으로 정한다. 처음 도착한 오래된 날짜를 배치 중간에 정리해 일부만 저장하는 문제도 테스트한다.

클라이언트는 이 오류를 구분하고 미전송 데이터를 보존하며 사용자에게 동기화 한계를 표시한다. 오래된 날짜 하나의 영구 오류가 오늘 전송을 막지 않도록 날짜별 격리/현재 날짜 우선 처리를 함께 설계한다. 구버전은 명시적 오류를 받되 과거 데이터가 되살아나거나 성공으로 오인되지 않아야 한다.

### 3. 서버 검증 및 적용

먼저 migration과 rollback SQL 회귀 파일을 완성한다. 합계 전후 동일, tombstone 부활 방지, 오래된 배치 재전송 거부, 권한, 방 탈퇴 cascade, 경계 날짜, 제한 건수, 다중 컬렉션, 동시 업로드 잠금을 검사한다. 실제 PostgreSQL 검증 없이 문자열 테스트만으로 완료 처리하지 않는다.

로컬 `psql`/`docker`는 이번 확인에서 PATH에 없었다. Supabase SQL 편집기 로그인 세션은 접근 가능했다. 운영 DB에서 검증한다면 임시 고유 계정/방을 사용하고 전체 rollback으로 기존 데이터를 보존한다. 실행 전 SQL을 검토하고 테스트 객체가 남지 않는지 확인한다.

2026-10-04 이번 작업에서는 아래 읽기 전용 쿼리만 실행 요청했다. 중단 때문에 결과는 아직 읽지 않았다:

```sql
begin read only;
select (now() at time zone 'Asia/Seoul')::date as today,
       count(*) as raw_rows, min(study_day) as oldest_day,
       count(*) filter (
         where study_day < (now() at time zone 'Asia/Seoul')::date - 90
       ) as archive_candidates
from public.anki_review_events;
rollback;
```

브라우저 기존 탭: `https://supabase.com/dashboard/project/uvrnsdknkivtclzlfjxx/sql/545c299e-5300-4c7d-8940-a51aa1be1736`.

자동 정리는 함수 검증과 배포 후 별도 단계로 설정한다. Supabase의 pg_cron을 사용한 일일 제한 배치가 후보이며 아직 확장 설치/스케줄 등록은 하지 않았다. 작업 실행 이력 자체도 무한히 쌓이지 않게 점검한다. 원본 삭제를 수반하므로 실제 활성화 전 대상·복구 방법·사용자 승인 범위를 확인한다. 단순 함수 추가만으로 자동 정리 완료라고 하지 않는다.

공식 참고: https://supabase.com/docs/guides/cron , https://supabase.com/docs/guides/cron/install , https://supabase.com/docs/guides/cron/quickstart .

### 4. 배포 정리

전체 Python 회귀 → Anki 번들 Qt panel/dock/settings smoke → 서버 SQL 검증 → 문서/버전/변경 내역 → 패키지 생성 → 12개 파일 소스/패키지 해시 → 설치 백업/교체 → Git commit/push 순으로 진행한다. 승인된 GitHub 반영 대상은 기존 origin 저장소다. 사용자 데이터·토큰·백업·화면 캡처를 커밋하지 않는다.

현재 manifest에는 버전이 없다. Anki manifest 지원 규칙을 확인하고 버전 표기와 changelog를 추가한다. GitHub Release 생성과 AnkiWeb 공개 업로드는 Git push와 다른 작업으로 명시한다.

물리 기기 검증은 `RELEASE_CHECKLIST.md`로 진행한다. 모의 두 클라이언트 테스트와 실제 PC/모바일 동기화 성공을 혼동하지 않는다. 장시간 사용자 테스트를 자동 테스트 통과만으로 완료 표시하지 않는다.

## 후속 추천 — 필수 작업 뒤

- 반복 통신 오류 때 지수 backoff, 성공하면 초기화. 서버 연결 불능 동안 접속 표시 만료는 정상이며 무한 heartbeat를 강요하지 않는다.
- 용량/전송량 알림. 별도 지속 모니터링은 아직 설정하지 않았다.
- 계정 복구 코드 안내와 마지막 성공 동기화/미전송 진단 표시.
- 모바일 웹·리더보드·사진·재촉 기능은 현재 배포 안정화 범위 밖이다.

## 검증 실행 환경

작업 폴더: `C:\Users\yoon\Documents\Codex\anki`.

```powershell
& 'C:\Users\yoon\Downloads\BN004908_total\.tools\python-3.13.15-embed-amd64\python.exe' -c "import sys,unittest; sys.path.insert(0,r'C:\Users\yoon\Documents\Codex\anki'); s=unittest.defaultTestLoader.discover('tests',pattern='test_*.py'); r=unittest.TextTestRunner(verbosity=1).run(s); raise SystemExit(not r.wasSuccessful())"
```

embedded Python은 기본 프로젝트 검색 경로를 추가해야 한다. 실제 Qt 검사는 `tests/native_panel_smoke.py`, `tests/native_dock_smoke.py`, `tests/native_settings_smoke.py`를 사용한다. Anki 설치 경로 읽기/교체와 Git 쓰기는 환경 권한 승인이 필요할 수 있다.
