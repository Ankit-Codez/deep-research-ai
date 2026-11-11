"""Revocable opaque-cookie login with Argon2 and session-bound CSRF tokens."""
import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pwdlib import PasswordHash

from deepresearch_ai.models.auth import AuthResponse, LoginRequest, RegisterRequest, UserPublic
from deepresearch_ai.models.account import DeleteAccountRequest
from deepresearch_ai.storage.auth import RateLimited, SQLAuthRepository

passwords = PasswordHash.recommended()
_DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))


def csrf_token(raw: str) -> str:
    return hmac.new(raw.encode(), b'deepresearch-csrf-v1', hashlib.sha256).hexdigest()


def public_user(user):
    return UserPublic(id=user.id, email=user.email, name=user.name, is_admin=user.is_admin)


def build_auth(settings, sessions):
    store = SQLAuthRepository(sessions)
    router = APIRouter(prefix='/auth', tags=['Authentication'])
    cookie = '__Host-dr_session' if settings.auth_cookie_secure else 'dr_session'
    pre_cookie = '__Host-dr_preauth' if settings.auth_cookie_secure else 'dr_preauth'

    def deny(status, code, message):
        raise HTTPException(status, detail={'code': code, 'message': message})

    def origin_check(request):
        origin = request.headers.get('origin')
        if origin and origin not in settings.auth_allowed_origins:
            deny(403, 'csrf_failed', 'The request origin is not allowed.')

    def check_csrf(request, raw):
        origin_check(request)
        supplied = request.headers.get('x-csrf-token', '')
        if not raw or len(raw) > 128 or len(supplied) != 64 or not supplied.isascii() or not hmac.compare_digest(supplied, csrf_token(raw)):
            deny(403, 'csrf_failed', 'Refresh your session before trying again.')

    def current_user(request: Request):
        user = store.user_for_session(request.cookies.get(cookie))
        if user is None:
            deny(401, 'authentication_required', 'Sign in to access your research.')
        return public_user(user)

    def mutation_user(request: Request, user: UserPublic = Depends(current_user)):
        check_csrf(request, request.cookies.get(cookie))
        return user

    def issue(response, user, previous):
        store.revoke_session(previous)
        raw = secrets.token_urlsafe(32)
        store.create_session(user.id, raw, settings.auth_session_hours)
        response.set_cookie(cookie, raw, max_age=settings.auth_session_hours * 3600,
            httponly=True, secure=settings.auth_cookie_secure, samesite='lax', path='/')
        response.delete_cookie(pre_cookie, httponly=True, secure=settings.auth_cookie_secure, samesite='lax', path='/')
        return AuthResponse(user=public_user(user), csrf_token=csrf_token(raw))

    def throttle(request, email=None, registering=False):
        ip = request.client.host if request.client else 'unknown'
        limit = settings.auth_rate_limit_attempts
        keys = [(f'register:{ip}', min(5, limit))] if registering else [
            (f'login:{ip}:{email}', limit), (f'login-ip:{ip}', limit * 3)]
        try: store.throttle(keys, settings.auth_rate_limit_window_seconds)
        except RateLimited:
            raise HTTPException(429, detail={'code': 'auth_rate_limited', 'message': 'Too many sign-in attempts. Try again later.'},
                                headers={'Retry-After': str(settings.auth_rate_limit_window_seconds)})

    @router.get('/csrf')
    def preauth(response: Response):
        raw = secrets.token_urlsafe(32)
        response.set_cookie(pre_cookie, raw, max_age=600, httponly=True,
                            secure=settings.auth_cookie_secure, samesite='lax', path='/')
        return {'csrf_token': csrf_token(raw)}

    @router.post('/register', response_model=AuthResponse, status_code=201)
    def register(payload: RegisterRequest, request: Request, response: Response):
        check_csrf(request, request.cookies.get(pre_cookie))
        if not settings.auth_allow_registration:
            deny(403, 'registration_disabled', 'Account registration is disabled.')
        throttle(request, registering=True)
        user = store.create_user(str(payload.email), payload.name, passwords.hash(payload.password.get_secret_value()))
        if user is None:
            deny(409, 'registration_failed', 'An account could not be created with these details.')
        return issue(response, user, request.cookies.get(cookie))

    @router.post('/login', response_model=AuthResponse)
    def login(payload: LoginRequest, request: Request, response: Response):
        check_csrf(request, request.cookies.get(pre_cookie))
        throttle(request, str(payload.email))
        user = store.find_user(str(payload.email))
        valid, replacement = passwords.verify_and_update(payload.password.get_secret_value(), user.password_hash if user else _DUMMY_HASH)
        if not valid or user is None or not user.is_active:
            deny(401, 'invalid_credentials', 'Invalid email or password.')
        if replacement: store.update_password_hash(user.id, user.password_hash, replacement)
        return issue(response, user, request.cookies.get(cookie))

    @router.get('/me', response_model=AuthResponse)
    def me(request: Request, user: UserPublic = Depends(current_user)):
        return AuthResponse(user=user, csrf_token=csrf_token(request.cookies[cookie]))

    @router.delete('/me', status_code=204)
    def delete_account(payload: DeleteAccountRequest, request: Request, response: Response,
                       user: UserPublic = Depends(mutation_user)):
        throttle(request, user.email)
        stored = store.find_user(user.email)
        if stored is None or not passwords.verify(payload.password.get_secret_value(), stored.password_hash):
            deny(401, 'invalid_credentials', 'Invalid email or password.')
        if not store.delete_account(str(user.id), stored.password_hash):
            deny(409, 'account_changed', 'Your account changed. Sign in again before deleting it.')
        response.delete_cookie(cookie, httponly=True, secure=settings.auth_cookie_secure, samesite='lax', path='/')
        response.delete_cookie(pre_cookie, httponly=True, secure=settings.auth_cookie_secure, samesite='lax', path='/')

    @router.post('/logout', status_code=204)
    def logout(request: Request, response: Response, user: UserPublic = Depends(mutation_user)):
        store.revoke_session(request.cookies.get(cookie))
        response.delete_cookie(cookie, httponly=True, secure=settings.auth_cookie_secure, samesite='lax', path='/')

    return router, current_user, mutation_user
