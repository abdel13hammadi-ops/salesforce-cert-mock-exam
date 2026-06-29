-- V46 additive fix: restrict free-mock curation RPCs to service_role only.
-- The foundation migration revoked PUBLIC on helper RPCs but did not explicitly
-- revoke EXECUTE from anon/authenticated, leaving them callable from the client.

REVOKE EXECUTE ON FUNCTION public.free_mock_blueprint_v1(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.free_mock_blueprint_v1(text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.free_mock_blueprint_v1(text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.free_mock_blueprint_v1(text) TO service_role;

REVOKE EXECUTE ON FUNCTION public.validate_free_mock_question_eligibility_v1(integer, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.validate_free_mock_question_eligibility_v1(integer, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.validate_free_mock_question_eligibility_v1(integer, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.validate_free_mock_question_eligibility_v1(integer, text, text) TO service_role;

REVOKE EXECUTE ON FUNCTION public.collect_free_mock_draft_failures_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.collect_free_mock_draft_failures_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.collect_free_mock_draft_failures_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.collect_free_mock_draft_failures_v1(uuid) TO service_role;

REVOKE EXECUTE ON FUNCTION public.create_free_mock_draft_v1(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.create_free_mock_draft_v1(text, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.create_free_mock_draft_v1(text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.create_free_mock_draft_v1(text, text, text) TO service_role;

REVOKE EXECUTE ON FUNCTION public.replace_free_mock_draft_items_v1(uuid, jsonb, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.replace_free_mock_draft_items_v1(uuid, jsonb, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.replace_free_mock_draft_items_v1(uuid, jsonb, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.replace_free_mock_draft_items_v1(uuid, jsonb, text) TO service_role;

REVOKE EXECUTE ON FUNCTION public.validate_free_mock_draft_v1(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.validate_free_mock_draft_v1(uuid) FROM anon;
REVOKE EXECUTE ON FUNCTION public.validate_free_mock_draft_v1(uuid) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.validate_free_mock_draft_v1(uuid) TO service_role;

REVOKE EXECUTE ON FUNCTION public.publish_free_mock_draft_v1(uuid, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.publish_free_mock_draft_v1(uuid, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.publish_free_mock_draft_v1(uuid, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.publish_free_mock_draft_v1(uuid, text, text) TO service_role;

REVOKE EXECUTE ON FUNCTION public.get_free_mock_curation_state_v1(text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_free_mock_curation_state_v1(text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.get_free_mock_curation_state_v1(text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.get_free_mock_curation_state_v1(text, text) TO service_role;
