# 공개 서버 API 계약

이 문서는 PC 애드온과 향후 웹 클라이언트가 공유하는 Supabase 계약의 요약이다. 모든 RPC는 `authenticated` 사용자 JWT가 필요하고, 방 데이터를 읽거나 쓰는 함수는 현재 사용자가 그 방의 멤버인지 확인한다. `anon` 프로젝트 키만으로 사용자 권한을 얻을 수는 없다.

날짜는 방의 IANA `time_zone`에서 04:00에 바뀐다. 클라이언트가 임의로 한국 시간이나 자정을 적용하면 안 된다.

| RPC | 인자 | 결과 | 권한·주요 오류 |
|---|---|---|---|
| `create_study_group` | `group_name`, `room_timezone` | `group_id`, 4자리 `invite_code`, `time_zone`, `day_start_hour` | 인증 사용자. 올바르지 않은 이름·시간대 거부 |
| `join_study_group_safe` | `code` | 방 JSON | 인증 사용자. 요청 제한, 없는 코드, 8명 상한, 차단 사용자 거부 |
| `leave_study_group` | `target_group` | 없음 | 본인만 탈퇴. 이미 비멤버이거나 없는 방은 성공인 멱등 처리 |
| `rotate_study_group_invite` | `target_group` | 새 초대 코드 | 방장만 가능 |
| `moderate_study_group_member` | `target_group`, `target_user`, `blocked` | 없음 | 방장만 가능. 내보내기·재입장 차단 또는 차단 해제 |
| `list_study_group_bans` | `target_group` | 차단된 `user_id` 목록 | 방장만 가능 |
| `sync_review_day` | `target_group`, `source_collection`, `target_day`, `reviews`, `removed_ids` | 해당 날짜의 이벤트 수·시간 합계·갱신 시각 | 본인 데이터만. 방 멤버, 날짜·event 구조·답변 시간 상한 검증 |
| `record_device_day` | `target_group`, `source_device`, `target_day`, `snapshot_revision`, `seconds_total`, `answers_total`, `activity_status`, `time_goal`, `cards_goal` | 저장된 기기 스냅샷 | 본인 기기 행만. 감소하지 않는 revision, 상태·목표 범위 검증 |
| `get_group_device_stats` | `target_group`, `target_day` | 멤버별 시간·답변·목표·상태 | 방 멤버만. 공개 합계는 동기화 복습 원본을 우선 사용 |
| `set_current_deck` | `target_group`, `source_device`, `deck_name` | 없음 | 본인 현재 덱만. `null`은 공유값 제거, 최대 300자 |
| `get_group_current_decks` | `target_group` | 멤버별 덱 이름·갱신 시각 | 방 멤버만 |
| `get_group_activity_timeline` | `target_group`, `target_day` | 멤버별 15분 활동 구간·기록 확인 가능 여부 | 방 멤버만, 방 04:00 경계 날짜 사용 |

## 테이블 조회 계약

- `profiles`: 사용자는 자기 프로필을 만들고 수정할 수 있다. 조회는 같은 방에서 만나는 사용자 범위로 제한된다.
- `group_members`: 방 멤버는 같은 방의 멤버십을 조회할 수 있다. 참여·탈퇴·차단은 위 RPC를 사용한다.
- `daily_stats`: 클라이언트 직접 INSERT/UPDATE는 지원하지 않는다. 이전 웹 구현은 `record_device_day`와 `sync_review_day`로 전환해야 한다.

## HTTP 계정 API

아이디 로그인·연결·복구의 HTTP 계약과 CORS 헤더는 [ACCOUNT_AUTH_API.md](ACCOUNT_AUTH_API.md)에 별도로 기록한다. 서비스 역할 키는 브라우저나 애드온에 포함하지 않는다.

## 배포 규칙

계약 변경은 새 migration으로 추가하고 `CHANGELOG.md`의 `⚠ 호환 깨짐`에 이전 방식, 새 방식, 클라이언트 조치, 운영 적용 여부를 적는다. 소스에 migration이 존재하는 것만으로 운영 적용이 완료된 것은 아니다.
