``` python
"""
Tests for cloudflare_access.py's JWT verification logic.

These tests do NOT talk to real Cloudflare. Instead, they:
  1. Generate a throwaway RSA keypair.
  2. Sign a JWT with the private key, shaped like a real Cloudflare
     Access token (email, aud, exp claims + a "kid" header).
  3. Mock requests.get() so that when the app fetches the JWKS
     endpoint, it gets back OUR test public key instead of Cloudflare's.
  4. Call the real verify_access_jwt()/get_authenticated_email()
     functions and check they behave correctly for both valid and
     invalid tokens.

Install: pip install pytest pyjwt cryptography
Run:     pytest test_cloudflare_access.py
"""

import time
import jwt
from jwt.algorithms import RSAAlgorithm
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

import cloudflare_access as ca


TEST_KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture
def mock_jwks(monkeypatch, rsa_keypair):
    """Makes ca.get_jwks() return our test public key instead of fetching it."""
    _, public_key = rsa_keypair

    # RSAAlgorithm.to_jwk gives us the public key in JWK format,
    # same shape Cloudflare's real /cdn-cgi/access/certs endpoint returns.
    import json
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(public_key))
    jwk_dict["kid"] = TEST_KID

    monkeypatch.setattr(ca, "get_jwks", lambda: [jwk_dict])


def make_token(private_key, *, email="user@example.com", aud=None, expired=False):
    aud = aud or ca.AUD_TAG
    payload = {
        "email": email,
        "aud": aud,
        "exp": int(time.time()) + (-3600 if expired else 3600),
        "iat": int(time.time()) - 10,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": TEST_KID},
    )


def test_valid_token_returns_email(rsa_keypair, mock_jwks):
    private_key, _ = rsa_keypair
    token = make_token(private_key, email="you@example.com")

    claims = ca.verify_access_jwt(token)

    assert claims["email"] == "you@example.com"


def test_expired_token_is_rejected(rsa_keypair, mock_jwks):
    private_key, _ = rsa_keypair
    token = make_token(private_key, expired=True)

    with pytest.raises(jwt.ExpiredSignatureError):
        ca.verify_access_jwt(token)


def test_wrong_audience_is_rejected(rsa_keypair, mock_jwks):
    private_key, _ = rsa_keypair
    token = make_token(private_key, aud="some-other-app")

    with pytest.raises(jwt.InvalidAudienceError):
        ca.verify_access_jwt(token)


def test_tampered_signature_is_rejected(rsa_keypair, mock_jwks):
    private_key, _ = rsa_keypair
    token = make_token(private_key)
    tampered = token[:-5] + "aaaaa"  # corrupt the signature

    with pytest.raises(Exception):
        ca.verify_access_jwt(tampered)


def test_get_authenticated_email_with_no_cookie(monkeypatch):
    """Simulates a request with no CF_Authorization cookie at all."""
    with ca.app.test_request_context("/"):
        # No cookie set -> should return None, not raise.
        assert ca.get_authenticated_email() is None


def test_get_authenticated_email_with_valid_cookie(rsa_keypair, mock_jwks):
    private_key, _ = rsa_keypair
    token = make_token(private_key, email="you@example.com")

    with ca.app.test_request_context("/", headers={"Cookie": f"CF_Authorization={token}"}):
        assert ca.get_authenticated_email() == "you@example.com"


def test_is_edit_mode_respects_allowed_emails(rsa_keypair, mock_jwks, monkeypatch):
    private_key, _ = rsa_keypair
    monkeypatch.setattr(ca, "ALLOWED_EMAILS", {"you@example.com"})

    allowed_token = make_token(private_key, email="you@example.com")
    other_token = make_token(private_key, email="stranger@example.com")

    with ca.app.test_request_context("/", headers={"Cookie": f"CF_Authorization={allowed_token}"}):
        assert ca.is_edit_mode() is True

    with ca.app.test_request_context("/", headers={"Cookie": f"CF_Authorization={other_token}"}):
        assert ca.is_edit_mode() is False
```