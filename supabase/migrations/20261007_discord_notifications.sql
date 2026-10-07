-- Migration: 20261007_discord_notifications.sql
-- Discord Webhook notification on new user profile registration

begin;

create or replace function public.notify_discord_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $func$
declare
  webhook_url text := 'https://discord.com/api/webhooks/1557365015550627881/b85agB-1moLyMf8U9pkHGkRZji35OP4HFF5br3FoASZV8ySalYaFvS4toVRvTCRZh2m7';
  kst_time text := to_char(now() at time zone 'Asia/Seoul', 'YYYY-MM-DD HH24:MI:SS');
  payload jsonb;
begin
  payload := jsonb_build_object(
    'embeds', jsonb_build_array(
      jsonb_build_object(
        'title', '🎉 신규 유저 접속 / 가입 알림',
        'description', '새로운 사용자가 Anki Study Room에 처음 등록되었습니다.',
        'color', 5814783,
        'fields', jsonb_build_array(
          jsonb_build_object('name', '닉네임 / 식별 코드', 'value', coalesce(NEW.display_name, '미설정'), 'inline', true),
          jsonb_build_object('name', '가입 시각 (KST)', 'value', kst_time, 'inline', true)
        ),
        'footer', jsonb_build_object('text', 'Anki Study Room')
      )
    )
  );

  begin
    perform net.http_post(
      url := webhook_url,
      body := payload,
      headers := jsonb_build_object('Content-Type', 'application/json')
    );
  exception when others then
    -- Do not fail profile creation if discord webhook has issues
    null;
  end;

  return NEW;
end;
$func$;

drop trigger if exists tr_notify_discord_new_user on public.profiles;

create trigger tr_notify_discord_new_user
after insert on public.profiles
for each row
execute function public.notify_discord_new_user();

commit;
