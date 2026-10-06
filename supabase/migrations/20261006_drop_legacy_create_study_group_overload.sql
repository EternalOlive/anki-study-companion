-- Drop legacy 2-argument overload of create_study_group to resolve PostgREST PGRST203 ambiguity
-- The 3-argument version (group_name, room_timezone, room_is_public) defaults room_is_public to false,
-- allowing both 2-argument and 3-argument calls to resolve cleanly.

DROP FUNCTION IF EXISTS public.create_study_group(text, text);
