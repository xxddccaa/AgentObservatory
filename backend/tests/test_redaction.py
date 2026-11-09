from agent_observatory.services.redaction import redact_secrets


def test_redacts_url_credentials_cookies_and_assignments() -> None:
    value = (
        "postgresql://user:secret-password@db/app "
        "redis://default:redis-secret@cache/0 "
        "Cookie: codex_obs_session=raw-cookie "
        "password=plain-secret"
    )
    redacted = redact_secrets(value)
    assert redacted is not None
    for secret in ("secret-password", "redis-secret", "raw-cookie", "plain-secret"):
        assert secret not in redacted
