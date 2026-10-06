-- Enforce alphanumeric display name constraints on public.profiles
-- User nicknames: 2-16 characters, alphanumeric only ([a-zA-Z0-9])
-- Default guest codes: 7 characters (XXX-XXX format with CODE_ALPHABET)

UPDATE public.profiles
SET display_name = 'AuthVerify'
WHERE display_name = 'Auth verification';

UPDATE public.profiles
SET display_name = 'FriendVerify'
WHERE display_name = 'Friend verification';

UPDATE public.profiles
SET display_name = 'NCX-6LA'
WHERE id = '9657d307-dece-4825-ac5d-4b87806cf599';

UPDATE public.profiles
SET display_name = 'Guest'
WHERE display_name !~ '^[a-zA-Z0-9]{2,16}$'
  AND display_name !~ '^[2-9A-HJ-NP-Z]{3}-[2-9A-HJ-NP-Z]{3}$';

ALTER TABLE public.profiles
  DROP CONSTRAINT IF EXISTS profiles_display_name_check;

ALTER TABLE public.profiles
  DROP CONSTRAINT IF EXISTS profiles_display_name_format_check;

ALTER TABLE public.profiles
  ADD CONSTRAINT profiles_display_name_format_check
  CHECK (
    display_name ~ '^[a-zA-Z0-9]{2,16}$'
    OR display_name ~ '^[2-9A-HJ-NP-Z]{3}-[2-9A-HJ-NP-Z]{3}$'
  );
