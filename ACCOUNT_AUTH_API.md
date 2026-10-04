# Account Auth HTTP API

`account-auth`는 아이디 계정 연결·로그인·복구를 제공하는 Supabase Edge Function이다. 공개 HTTP 계약은 아래와 같다.

## 요청

- 경로: `/functions/v1/account-auth`
- 허용 메서드: `POST`, `OPTIONS`
- `POST` 본문: UTF-8 JSON 객체, 최대 4096바이트
- 공통 필드: `action`, `username`, `password`
- `action`: `bind`, `login`, `recover` 중 하나
- `recover`는 `recovery_code`도 필요하다.
- `bind`는 현재 사용자의 Bearer 토큰이 필요하다. `login`과 `recover`는 함수 게이트웨이 설정에 필요한 공개 키 헤더 외에 사용자 세션을 요구하지 않는다.

`POST` 외 실제 작업 메서드는 JSON `405`를 반환한다. 브라우저의 `OPTIONS` 사전 요청은 본문 파싱, 사용자 인증, 요청 제한, 외부 호출 전에 `204`로 끝난다.

## CORS 응답

사전 요청과 모든 JSON 성공·오류 응답은 다음 계약을 공유한다.

- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Headers: authorization, x-client-info, apikey, content-type`
- `Access-Control-Allow-Methods: POST, OPTIONS`
- `Access-Control-Max-Age: 86400`

쿠키 기반 자격 증명은 이 계약에 포함하지 않는다. 클라이언트는 기존처럼 `Authorization`과 `apikey` 헤더를 사용한다.

## 호환성 및 배포

이번 CORS 변경의 breaking change는 없다. 변경 전 함수도 `POST` 외 메서드를 `405`로 거부했고, 현재 PC 애드온은 `study_companion/online.py`에서 이 경로를 `POST`로 호출한다. 새 동작은 브라우저에 필요한 `OPTIONS`만 추가한다. 요청·응답 JSON과 데이터베이스 RPC는 바뀌지 않으므로 DB migration도 필요하지 않다.

소스와 회귀 테스트만 준비된 상태이며 운영 Edge Function에는 별도 배포가 필요하다. 배포 전후에는 `tests/account_auth_runtime.mjs`로 `OPTIONS`, `GET`, 성공 및 오류 응답의 헤더 계약을 확인한다.
