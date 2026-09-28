from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_web_auth_uses_pkce_cookie_fallback_and_server_exchange():
    auth = (ROOT / "web/src/auth.ts").read_text(encoding="utf-8")
    app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")

    assert 'flowType: "pkce"' in auth
    assert 'detectSessionInUrl: false' in auth
    assert "-code-verifier" in auth
    assert "SameSite=Lax; Secure" in auth
    assert "exchangeCodeForSession" in auth
    assert "exchangePublicAuth" in auth
    assert 'signOut({ scope: "local" })' in auth
    assert "recoverPublicAuth" in auth
    assert "startPublicAuth" in app
    assert "Войти через Яндекс" in app


def test_product_does_not_embed_yandex_client_secret_or_service_role_key():
    roots = [
        ROOT / "src/projects_hub",
        ROOT / "web/src",
        ROOT / "deploy/devcoveer_install.py",
    ]
    text = ""
    for root in roots:
        if root.is_dir():
            text += "\n".join(
                path.read_text(encoding="utf-8")
                for path in root.rglob("*")
                if path.is_file()
                and path.suffix in {".py", ".ts", ".tsx"}
            )
        else:
            text += root.read_text(encoding="utf-8")
    assert "YANDEX_CLIENT_SECRET" not in text
    assert "SUPABASE_SERVICE_ROLE_KEY" not in text