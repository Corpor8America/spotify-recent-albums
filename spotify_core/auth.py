"""OAuth (web-flow) helpers and refresh-token persistence."""

from urllib.parse import urlencode

import requests

from .errors import AuthError


def get_auth_url(ctx, client_id, redirect_uri, csrf_state):
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "user-follow-read playlist-modify-public playlist-modify-private",
        "state": csrf_state,
        "show_dialog": "false",
    }
    return f"{ctx.spotify_auth_url}?{urlencode(params)}"


def exchange_code_for_token(ctx, client_id, client_secret, code, redirect_uri):
    resp = requests.post(ctx.spotify_token_url, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=(5, 30))
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise AuthError(str(e)) from e
    return resp.json()  # contains access_token + refresh_token


def get_access_token(ctx, client_id, client_secret, refresh_token):
    resp = requests.post(ctx.spotify_token_url, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=(5, 30))
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise AuthError(str(e)) from e
    return resp.json()["access_token"]


def save_refresh_token(ctx, refresh_token):
    ctx.store.save_refresh_token(refresh_token)


def load_refresh_token(ctx):
    return ctx.store.load_refresh_token()


def is_connected(ctx):
    return load_refresh_token(ctx) is not None
