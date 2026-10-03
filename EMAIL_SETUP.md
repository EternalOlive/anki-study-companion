# 인증 메일 발송 설정

현재 프로젝트는 Supabase 기본 메일을 사용합니다. 일반 사용자에게 보내려면
별도 SMTP를 연결해야 합니다. 메일 인증을 끄면 해결되는 문제가 아닙니다.

## 도메인이 없을 때: Brevo로 소규모 테스트

1. https://www.brevo.com 에서 계정을 만듭니다.
2. 발신자로 사용할 본인 이메일을 등록하고, 받은 인증 코드로 확인합니다.
3. SMTP 설정에서 SMTP login과 SMTP key를 확인합니다. API key가 아닙니다.
4. Supabase → Authentication → Emails → SMTP Settings에서 입력합니다.
   - Host: `smtp-relay.brevo.com`
   - Port: `587`
   - Username: Brevo의 SMTP login
   - Password: Brevo의 SMTP key
   - Sender email: 인증한 발신 이메일
   - Sender name: `Anki Study Companion`
5. 프로젝트 팀원이 아닌 별도 이메일로 인증 요청을 보내 실제 수신을 확인합니다.
6. 메일 확인 후 Anki에서 비밀번호를 설정하고, 다른 PC에서 로그인합니다.

비밀키나 비밀번호는 채팅·GitHub·애드온에 넣지 말고 Supabase 설정 화면에만
입력하세요. 가입 약관 동의와 인증 코드 입력은 계정 소유자가 진행합니다.

무료 발신 주소는 전달률·스팸 분류에 제한이 있을 수 있습니다. 공개 운영이
커지면 전용 도메인 인증이 필요합니다. 링크 확인 후 localhost 오류가 뜬다면
Supabase의 Site URL도 실제 안내 페이지로 변경해야 합니다. 아직 해당 메일
발송/수신과 링크 이동의 실사용 검증은 완료하지 않았습니다.

## 공식 안내

- https://supabase.com/docs/guides/auth/auth-smtp
- https://help.brevo.com/hc/en-us/articles/7924908994450-Send-transactional-emails-using-Brevo-SMTP
- https://help.brevo.com/hc/en-us/articles/14925263522578-Comply-with-Gmail-Yahoo-and-Microsoft-s-requirements-for-email-senders
