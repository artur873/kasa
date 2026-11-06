-- 0015_definer_grants: SECURITY DEFINER helpers of the agent's views are callable only by kasa's roles.
--
-- The helpers below run as their owner (they read tables the agent's roles cannot). Until now EXECUTE was left
-- to PUBLIC; on Supabase that means the anonymous REST API could call them (/rest/v1/rpc/kasa_party_display
-- returned a counterparty's display name for a guessed id - advisor lint 0028/0029, 6.11.2026). They are needed
-- only by the agent's roles (views and metrics computed as kasa_agent_ro, the subject guard on INSERT of
-- kasa_agent_suggest); the owner keeps them as every owner does. Nothing else changes.

DO $$
DECLARE
    v_fn text;
    v_role text;
BEGIN
    FOREACH v_fn IN ARRAY ARRAY[
        'kasa_tx_is_payroll(uuid)',
        'kasa_doc_is_person(text, uuid)',
        'kasa_doc_is_person(uuid)',
        'kasa_party_display(uuid)',
        'kasa_rank_reason_masked(text, subject_type, uuid)',
        'kasa_subject_exists(subject_type, uuid, uuid)'
    ] LOOP
        EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', v_fn);
        -- Supabase grants functions of public to its API roles explicitly (default privileges)
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
                EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM %I', v_fn, v_role);
            END IF;
        END LOOP;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO kasa_agent_ro, kasa_agent_suggest', v_fn);
    END LOOP;
END
$$;
