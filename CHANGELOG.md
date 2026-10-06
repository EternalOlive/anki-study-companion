# 변경 내역

## ⚠ 호환 깨짐 — 서버 migration 적용 시

아래 항목은 로컬 소스에 준비되어 있으며, 운영 Supabase에 해당 migration이 적용됐는지는 별도로 확인해야 한다.

- `20261003_activate_device_sync.sql`: 클라이언트의 `daily_stats` 직접 INSERT/UPDATE 권한을 제거했다. 기기 합계는 `record_device_day`를 사용한다.
- `20261004_invite_code_protection.sql`: 기존 참여 함수 대신 요청 제한과 차단 검사를 포함한 `join_study_group_safe`를 사용한다.
- `20261004_synced_review_totals.sql`: 공개 합계의 시간·답변 수는 동기화된 복습 이벤트를 기준으로 계산한다. `record_device_day`는 목표와 현재 상태의 보조 경로다.
- `20261004_room_time_zone.sql`: 날짜는 UTC나 한국 자정이 아니라 방 시간대의 매일 04:00 경계를 사용한다. 웹 클라이언트도 방의 `time_zone`과 `day_start_hour`로 날짜를 계산해야 한다.
- `20261004_leave_study_group_idempotent.sql`: 이미 내보내졌거나 나간 사용자의 반복 탈퇴 요청을 성공으로 처리한다. 다른 멤버·차단 기록은 변경하지 않는다.
- `20261006_room_management.sql`: 방 관리 RPC 4종(`update_room_timezone`, `transfer_room_ownership`, `kick_room_member`, `cleanup_inactive_members`)을 추가했다. 방장만 호출 가능하며 JWT 인증이 필요하다.
- `20261006_public_rooms.sql`: `study_groups` 테이블에 `is_public` 플래그 및 인덱스를 추가하고, 공개 방 목록을 필터링·추천 정렬하는 `list_public_study_groups(user_timezone)` RPC와 `create_study_group` 공개 플래그 파라미터를 추가했다.
- `20261006_ten_minute_timeline.sql`: `get_group_activity_timeline` RPC의 활동 집계 단위를 기존 15분(하루 96칸)에서 10분(하루 144칸)으로 단축했다.
- `20261006_display_name_validation.sql`: `public.profiles`의 `display_name`에 영문/숫자 2~16자(`^[a-zA-Z0-9]{2,16}$`) 또는 기본 익명 코드(`^[2-9A-HJ-NP-Z]{3}-[2-9A-HJ-NP-Z]{3}$`) 형식 제약 조건(`profiles_display_name_format_check`)을 추가했다.
- `20261006_member_pokes.sql`: 방 멤버끼리 찌르기 RPC 2종(`poke_room_member`, `fetch_my_pokes`)과 `member_pokes` 테이블을 추가했다. 기존 RPC는 바뀌지 않는다. 미적용 서버에서도 새 애드온은 찌르기 버튼만 숨기고 정상 동작한다.

웹 또는 별도 클라이언트는 [공개 API 계약](PUBLIC_API_CONTRACT.md)을 기준으로 RPC를 호출해야 한다. 익명 API 키는 프로젝트 식별용이며 사용자 권한을 대신하지 않으므로, 모든 사용자 작업에는 해당 사용자의 JWT가 필요하다.

## 찌르기 — 2026-10-06

요청: 방 친구들이 서로 찌를 수 있게.

- 패널의 친구 행 오른쪽에 작은 `찌르기` 버튼을 둔다. 내 행에는 없다. 누르면 그 친구 버튼만 60초 동안 꺼지고(서버 제한과 같다) `○○님을 콕 찔렀어요` 툴팁을 띄운다. 너무 자주 누르면 서버 제한 안내를 툴팁으로 보이며, 창을 띄우지 않는다.
- 약 30초 동기화마다 나에게 온 찌르기를 확인해 `○○님이 콕 찔렀어요` 툴팁을 띄운다. 한 사람이 여러 번 찌르면 `○○님(2번)`으로 묶는다. 이름은 친구 행과 같은 표시 이름(고유번호)이다. 패널이 숨겨져 있어도 확인한다.
- 서버에 찌르기 RPC가 없으면(404 / PGRST202) 오류 표시 없이 버튼을 숨기고 조회를 멈추며, 30분 뒤 다시 확인한다. 찌르기 조회 실패는 기록 전송 상태·재시도 간격에 영향을 주지 않는다.
- 패널 폭이 약 320px보다 좁으면 버튼 자리 때문에 친구 행이 기존보다 일찍 두 줄 배치로 바뀐다.
- 서버 계약: `supabase/migrations/20261006_member_pokes.sql` — `poke_room_member(target_group, target_user)`, `fetch_my_pokes(target_group, since)`. 상세는 [공개 API 계약](PUBLIC_API_CONTRACT.md)의 `찌르기 RPC`. 회귀 SQL은 `supabase/tests/member_pokes.sql`(TEST DB 전용, 롤백).
- 클라이언트: `SupabaseClient.poke_room_member`·`fetch_my_pokes`, `api.py` 내보내기, 미적용 서버 표시용 `PokeUnavailable`.

## 사용자 지정 닉네임 기능 및 보안 검증 강화 — 2026-10-06

- **사용자 지정 영문/숫자 닉네임 지원**:
  - 설정 창 계정 탭에 '내 닉네임' 설정 섹션(입력 필드, [변경], [기본 코드로] 리셋 버튼) 추가.
  - 2~16자의 영문 대소문자 및 숫자(`[a-zA-Z0-9]`)만 허용하며, UI 단에서 `QRegularExpressionValidator`를 통해 공백/특수문자/한글 입력을 원천 방지.
- **서버 및 피어 데이터 보안 검증 & Sanitization (`study_companion/nicknames.py`)**:
  - 서버(PostgREST) 및 WebSocket 피어 브로드캐스트/Presence로부터 수신되는 모든 `display_name`을 철저히 검증.
  - 형식에 맞지 않거나 XSS/스크립트/인젝션 시도 문자열이 감지될 경우, 안전하게 사용자의 기본 익명 코드(`canonical_nickname(user_id)`)로 자동 fallback하여 보안 사고 방지.
- **서버 DB 제약 조건 배포 (`supabase/migrations/20261006_display_name_validation.sql`)**:
  - `public.profiles` 테이블에 `profiles_display_name_format_check` CHECK 제약 조건을 추가하여 DB 수준에서도 비인가 문자열 저장을 차단.
- **프로필 동기화 및 타 기기 로그인 유지 로직 개선**:
  - 백그라운드 주기 동기화 시 커스텀 닉네임이 익명 코드로 덮어써지지 않도록 수정 (`synced_display_name` 추적).
  - 다른 PC 로그인 및 복구 시 서버에 저장되어 있던 기존 닉네임을 조회하여 안전하게 복원 유지.

## Supabase Realtime 웹소켓 도입 (저지연 피어 동기화 및 DB 부하 절감) — 2026-10-06

- **Supabase Realtime WebSocket 클라이언트 (`study_companion/realtime.py`)**:
  - Anki 번들 PyQt6의 `QWebSocket`을 활용하여 추가 외부 의존성 없이 네이티브 WSS 연결 구현.
  - **Broadcast**: 카드를 풀 때마다 실시간 복습 틱(`review_tick`: slot, answers, duration)을 방 참가자들에게 즉시(<50ms) 중계 (DB Write 0회).
  - **Presence & 상태 브로드캐스트**: 방 참가/퇴장, 공부 중/멈춤 상태, 현재 덱 이름을 실시간 중계하여 30초 폴링 대기 없이 UI에 즉각 반영.
  - Heartbeat(25초 주기) 및 네트워크 순단 시 지수 백오프 자동 재연결 지원.
- **REST DB 동기화 주기 완화 (Lazy DB Flush)**:
  - WebSocket 실시간 연결이 활성화된 경우 기존 30초 주기의 REST DB RPC 폴링을 300초(5분) 주기로 완화하여 DB 부하를 90% 이상 절감.
  - 세션 종료, Anki 종료, 웹소켓 연결 해제 시에는 즉시 DB로 안전하게 batch flush.
- **테스트 및 안정성**:
  - `tests/test_realtime.py` 단위 테스트 추가 (총 256개 테스트 전체 통과).

## 활동 타임라인 10분 구간 전환 — 2026-10-06

- **시간대 1등 및 활동 구간 간격 10분 단축**:
  - 타임라인 버킷을 기존 15분 단위(하루 96개 슬롯)에서 10분 단위(하루 144개 슬롯)로 단축.
  - 방 시간대 띠 명칭을 `방 시간대 · 10분 1등` (`Room timeline · top per 10 min`)으로 변경.
  - 개별 멤버 활동 시간대 서브타이틀 및 최대 높이 스케일을 `10:00` (`답변 시간 / 10분 구간`)으로 갱신.
  - X축 눈금 레이블 슬롯 매핑을 144개 슬롯(04시=0, 10시=36, 16시=72, 22시=108, 04시=144)으로 조정.
- **서버 RPC 마이그레이션 (`supabase/migrations/20261006_ten_minute_timeline.sql`)**:
  - `get_group_activity_timeline` RPC에서 슬롯 계산을 10분 간격(`mod(hour * 6 + floor(minute / 10) - day_start_hour * 6 + 144, 144)`)으로 수정.
  - 운영 Supabase DB에 migration 배포 완료.
- **클라이언트 및 단위 테스트**:
  - `study_companion/room_activity.py`의 `SLOTS_PER_DAY = 144` 갱신.
  - `study_companion/study_day.py`에 `ten_minute_slot` 헬퍼 함수 추가 (기존 `quarter_hour_slot` 호환 유지).
  - 단위 테스트(`test_room_activity.py`, `test_study_day.py`) 및 Qt6 네이티브 스모크 테스트(`native_panel_smoke.py`) 갱신 및 250개 전 항목 통과.

## 공개 스터디방 및 방 관리 — 2026-10-06

### 서버 및 데이터베이스 계약
- **공개 방 시스템 (`supabase/migrations/20261006_public_rooms.sql`)**:
  - `study_groups.is_public boolean default false` 컬럼 및 부분 인덱스 `idx_study_groups_public` 추가.
  - `create_study_group(group_name text, room_timezone text default 'Asia/Seoul', room_is_public boolean default false)`: 방 생성 시 공개 여부 인자 추가 (기본값 `false`).
  - `list_public_study_groups(user_timezone text default 'Asia/Seoul')`: 인원 8명 미만인 공개 방을 조회하며, 사용자 시간대 일치 우선 → 최근 90초 내 실시간 공부 중(`device_daily_stats.status = 'studying'`) 인원수 내림차순 → 총 멤버수 내림차순 → 생성일 최신순으로 정렬하여 최대 30개 반환. 호출자가 차단(`study_group_bans`)된 방은 제외.
- **방 관리 기능 (`supabase/migrations/20261006_room_management.sql`)**:
  - `update_room_timezone(target_group uuid, new_timezone text)`: 방장 권한으로 방 시간대 변경. 04:00 고정 기준일 불변성 검증 트리거(`guard_study_group_calendar`) 유지.
  - `transfer_room_ownership(target_group uuid, new_owner_id uuid)`: 해당 방의 기존 멤버에게 방장 권한 위임.
  - `kick_room_member(target_group uuid, target_user uuid)`: 멤버 강퇴 (해당 방의 `daily_stats` 및 `group_members` 행 삭제, 방장 본인은 강퇴 불가).
  - `cleanup_inactive_members(target_group uuid, days_inactive integer default 14)`: 지정 일수(기본 14일) 동안 동기화(`anki_review_day_markers`, `device_daily_stats`)가 없는 비방장 멤버 일괄 정리.

### 클라이언트 API (`study_companion/api.py`, `study_companion/online.py`)
- `SupabaseClient`에 `update_room_timezone`, `transfer_room_ownership`, `kick_room_member`, `cleanup_inactive_members`, `list_public_study_groups` 메서드 추가.
- `create_group`에 `is_public: bool = False` 선택 인자 지원 (하위 호환성 유지).
- `api.py` 모듈에서 상기 RPC 래퍼 함수들을 export하여 타 클라이언트 및 외부 모듈 연동 지원.

### UI / UX 개편 (`study_companion/settings.py`)
- **방 미참여 시 메인 화면 (클릭 0회 즉시 탐색)**:
  - '초대 코드로 참여'를 누르지 않아도, 설정의 '방' 탭 진입 시 공개 스터디방 목록이 메인 화면에 즉시 노출.
  - 상단: `[새 방 만들기]`, `[초대 코드로 참여]` 액션 버튼.
  - 중앙: 공개 스터디방 목록 헤더(`[새로고침]`, `[빠른 참여]`), 스크롤 가능한 실시간 공개 방 목록 (`방 이름`, `N명 공부 중`, `인원수/8`, `[참여]`).
- **1인 1방 규칙 연동 및 방 전환**:
  - 이미 방에 참여 중인 경우: 내 방 정보 하단에 `[다른 공개 방 둘러보기]` 버튼 제공.
  - 다른 방의 `[참여]` 클릭 시 기존 방 퇴장 확인 다이얼로그(`QMessageBox.question`)를 거쳐 기존 방 안전 퇴장 후 새 방으로 자동 전환.
- **간결한 표현 (설명적·감성적 텍스트 배제)**:
  - 방 만들기 체크박스: `공개 방으로 설정 (다른 사용자가 검색하여 참여 가능)`에서 설명 문구를 떼어내고 `공개 방으로 설정`으로 간결화.
  - 불필요한 하단 주석 안내문 제거.
- **다국어 (i18n)**:
  - 한국어(`ko`), 영어(`en`), 일본어(`ja`), 중국어 간체(`zh_CN`) 4개 국어 지원.

## 친구 요청 — 2026-10-05

- 친구 목록 위에 `방 시간대 · 15분 1등` 띠를 추가했다. 04→다음 04시 96칸마다 답변이 가장 많은 사람의 색으로 칠하고, 1등이 동점이면 회색, 기록 없는 칸은 비운다. 칸을 가리키거나 누르거나 좌우 방향키로 고르면 `HH:MM–HH:MM · 1등 이름 n회 · 2등 …`을 표시하고, 아래 범례에 사람별 `1등 n번`(동점 칸 제외)을 둔다. 색은 방 참여 순서로 고정하며 나는 테마 강조색이다. 추가 서버 조회는 없다.
- 덱 이름은 공부 중일 때만이 아니라 방에 접속해 있는 동안 덱이 바뀌기 전까지 계속 공유한다. 그날(방 04:00 기준) 아직 덱을 열지 않았으면 공유하지 않는다. 친구 행은 접속 끊김이 아니고 덱 갱신 시각이 오늘인 경우에만 덱을 표시한다. 재전송 주기는 60초에서 45초로 줄였다(서버가 90초 넘은 덱을 돌려주지 않아, 30초 동기화와 겹치면 덱이 잠깐 빠지던 틈을 막는다).
- 공개 상태는 마지막 입력 후 2분 동안 `공부 중`을 유지한다. 공부 시간은 기존대로 1분 무입력 시 멈추며 두 기준을 별도 상수로 분리했다. 친구는 180초(기존 90초) 갱신이 없으면 접속 끊김으로 본다.
- 방 머리글을 `n명 공부 중`에서 `n명 접속 중 · 나 포함`으로 바꿨다.
- 활동 시간대와 방 시간대 띠에 오늘의 현재 시각을 얇은 세로선으로 표시한다.
- 내 기록의 최근 7일 막대를 사람별 답변 수 선 그래프로 바꿨다. 나는 굵은 선이다. 지난 날짜는 `get_group_device_stats`로 날짜마다 한 번만 읽어 방·날짜별로 저장하고, 오늘은 기존 친구 조회 주기의 값을 쓴다. 패널이 숨겨져 있거나 친구 조회가 실패하면 읽지 않으며, 읽지 못한 날은 선을 끊는다. 날짜를 고르면 사람별 답변 수를 표시한다.
- 서버 계약·migration 변경 없음.

## 버그 수정 — 2026-10-05

- 활동 시간대에서 막대가 없는 곳에 마우스를 올리면 오늘 모든 15분 구간 설명이 한 줄로 이어진 긴 툴팁이 뜨던 문제를 고쳤다. 막대 위는 그 구간 설명, 빈 곳·바깥은 짧은 안내만 표시한다. 화면 낭독기용 전체 설명(accessibleName)은 그대로 둔다.

## UX 후속 — 2026-10-04

- 공부 중인 덱 이름 공유를 기본 활성화하고 친구 행에서 바로 표시한다. 사용자가 설정에서 끄면 숨긴다.
- 계정 인증 Edge Function의 브라우저 `OPTIONS` 사전 요청을 본문·인증·요청 제한 처리 전에 응답하고, 성공·오류를 포함한 모든 JSON 응답에 동일한 CORS 헤더를 추가했다. 기존 `POST` 클라이언트와 DB 계약은 변경하지 않았으며 아직 운영 서버에는 배포하지 않았다.
- 내 메인 공부 시간은 PC 복습 중 매초 증가하고 1분 무입력 시 멈춘다. 답변 수는 로컬 즉시 기록과 동기화된 Anki 기록을 함께 반영하며 서버 조회 횟수는 늘리지 않는다.
- 내 상태와 친구 상태를 동일한 작은 원으로 표시한다. 공부 중은 초록색, 접속 중은 파란색이며 확인 불가 상태는 숨긴다.
- 패널 위의 접기 버튼과 접힌 화면 오른쪽 가장자리의 펼치기 버튼을 제공하고 마지막 너비를 복원한다. 밝고 어두운 테마 모두에서 화살표와 얇은 테두리를 표시한다.
- 방 미참여 시 패널에서 생성·참여 화면으로 바로 이동한다.
- 내 시간·답변 수치를 눌러 목표를 독립적으로 설정·해제하고, 관리창의 수정하지 않은 값이 이를 덮어쓰지 않게 한다.
- 관리창에서 현재 초대 코드 또는 짧은 참여 안내를 복사한다.
- 기록 확인·저장·공유·친구 조회 상태를 구분하고, 관리창의 접힌 상세에 성공 시각을 제공한다. 복습 원본과 기기 대기열을 함께 확인하며 추가 서버 조회는 없다.

## 0.1.0 — 2026-10-04

- 방 생성 시 지역 시간대를 선택하고, 모든 방 기록은 해당 시간대의 04:00부터 다음 날 04:00까지 집계한다. 기존 방은 Asia/Seoul을 유지한다.
- 서머타임의 23·25시간 학습일을 처리하며, 시간대 그래프의 반복 시각은 같은 15분 구간에 합산한다.
- PC Anki의 오늘 답변 수·답변 시간, 목표, 현재 공부 상태를 소규모 비공개 방에서 공유한다.
- 익명 사용과 선택형 아이디·비밀번호 계정 연결, 4자리 초대 코드, 방 생성·참여·나가기를 제공한다.
- 친구 오늘 활동 시간띠, 개인 최근 7일 기록, 선택형 현재 덱 공유를 제공한다.
- 방장 전용 멤버 관리에서 사용자를 내보내고 같은 계정의 재참여를 차단하거나 차단을 해제할 수 있다.
- 시간대 막대 아래 선택 구간의 불필요한 고정 여백을 줄였다.
- 시간대 선택 정보의 위아래 여백을 동일하게 맞추고 패널 공통 간격을 14→8px, 친구 행 위아래를 7→5px로 축소한다.
- 한 방의 인원을 방장 포함 8명으로 제한하고 직접 삽입을 포함한 모든 참가 경로에 적용한다.
- 반복 오류의 자동 재시도 간격을 1·2·4·5분으로 늘리며, 수동 재시도는 즉시 실행한다.
- 패널이 숨겨진 동안 친구 목록·시간대 서버 조회와 숨겨진 패널 렌더링을 중지한다.
- 친구 조회를 90초 캐시하고 공부 중 덱만 60초마다 갱신하면서 30초 접속 상태 heartbeat는 유지한다.
- 새 방에는 어제·오늘 기록만 공유하고 기존 방의 미전송 기록은 성공할 때까지 보존한다.
- 계정·방·날짜가 바뀌거나 패널을 다시 펼치면 친구 목록을 즉시 새로 읽는다.
- 오래된 전송 완료 로컬 캐시를 정리하되 Anki 원본 revlog와 미전송·실행 취소 기록은 보존한다.
- 90일 이후 서버 상세 기록을 날짜별 합계로 보관하는 migration과 rollback 회귀 SQL을 준비했다. 운영 적용과 자동 예약은 이 버전에 포함하지 않는다.
