"""Validate the OAuth persistence module offline (no live auth probing).

Checks: SMART discovery against the FHIR base, PKCE S256 correctness, and the
authorize-URL shape. Does NOT hit the authorize/token endpoints (that needs a
real registered client_id).
"""
import base64, hashlib, urllib.parse

from maisa.oauth import OAuthConfig, discover, make_pkce, build_authorize_url, DEFAULT_SANDBOX_FHIR


def main():
    cfg = OAuthConfig(fhir_base=DEFAULT_SANDBOX_FHIR, client_id="<your-registered-client-id>")

    print("1) SMART discovery")
    meta = discover(cfg.fhir_base)
    for k in ("authorization_endpoint", "token_endpoint", "grant_types_supported",
              "code_challenge_methods_supported", "capabilities"):
        print(f"   {k}: {meta.get(k)}")
    assert "refresh_token" in meta.get("grant_types_supported", []), "no refresh_token grant!"
    assert "S256" in meta.get("code_challenge_methods_supported", []), "no S256 PKCE!"

    print("\n2) PKCE S256")
    verifier, challenge = make_pkce()
    recomputed = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert recomputed == challenge, "PKCE challenge mismatch"
    print(f"   verifier[{len(verifier)}] -> challenge[{len(challenge)}] OK")

    print("\n3) authorize URL")
    url = build_authorize_url(cfg, meta["authorization_endpoint"], "state123", challenge)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for k in ("response_type", "client_id", "redirect_uri", "scope", "aud",
              "code_challenge", "code_challenge_method"):
        assert k in q, f"missing {k}"
    print("   " + url[:120] + "…")
    print("   params:", sorted(q.keys()))

    print("\nAll offline checks passed. Live flow needs MAISA_OAUTH_CLIENT_ID "
          "(register at fhir.epic.com, enable for Apotti).")


if __name__ == "__main__":
    main()
