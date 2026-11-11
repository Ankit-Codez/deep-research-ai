"""Transactional account/session storage, sharing the application's DB engine."""
from datetime import timedelta
import hashlib
from uuid import uuid4

from sqlalchemy import case, delete, select, update
from sqlalchemy.exc import IntegrityError

from deepresearch_ai.storage.report_store import insert_if_missing
from deepresearch_ai.storage.sql import utcnow
from deepresearch_ai.storage.tables import RateLimitRow, SessionRow, UserRow


class RateLimited(Exception):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SQLAuthRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    def create_user(self, email, name, password_hash):
        row = UserRow(id=str(uuid4()), email=email, name=name, password_hash=password_hash,
                      is_active=True, created_at=utcnow())
        try:
            with self.sessions.begin() as session:
                session.add(row)
            return row
        except IntegrityError:
            return None

    def find_user(self, email):
        with self.sessions() as session:
            return session.scalar(select(UserRow).where(UserRow.email == email))

    def update_password_hash(self, user_id, old_hash, new_hash):
        with self.sessions.begin() as session:
            session.execute(update(UserRow).where(UserRow.id == user_id, UserRow.password_hash == old_hash)
                            .values(password_hash=new_hash))

    def delete_account(self, user_id, expected_password_hash):
        from deepresearch_ai.storage.deletion import delete_runs
        from deepresearch_ai.storage.tables import RunRow
        from deepresearch_ai.storage.usage import lock_user
        with self.sessions.begin() as session:
            lock_user(session, user_id)
            user = session.get(UserRow, user_id)
            if user.password_hash != expected_password_hash:
                return False
            rows = list(session.scalars(select(RunRow).where(RunRow.user_id == user_id)
                                        .order_by(RunRow.id).with_for_update()))
            delete_runs(session, rows)
            session.execute(delete(UserRow).where(UserRow.id == user_id))
            return True

    def create_session(self, user_id, token, hours):
        now = utcnow()
        with self.sessions.begin() as session:
            session.add(SessionRow(token_hash=digest(token), user_id=user_id, created_at=now,
                                   expires_at=now + timedelta(hours=hours)))

    def user_for_session(self, token):
        if not token or len(token) > 128: return None
        with self.sessions() as session:
            return session.scalar(select(UserRow).join(SessionRow, SessionRow.user_id == UserRow.id)
                .where(SessionRow.token_hash == digest(token), SessionRow.expires_at > utcnow(), UserRow.is_active.is_(True)))

    def revoke_session(self, token):
        if not token: return
        with self.sessions.begin() as session:
            session.execute(delete(SessionRow).where(SessionRow.token_hash == digest(token)))

    def throttle(self, limits, seconds):
        now = utcnow()
        cutoff = now - timedelta(seconds=seconds)
        with self.sessions.begin() as session:
            for key, limit in limits:
                key = digest(key)
                insert_if_missing(session, RateLimitRow,
                    {'key': key, 'window_start': now, 'attempts': 0}, ['key'])
                expired = RateLimitRow.window_start <= cutoff
                changed = session.execute(update(RateLimitRow).where(RateLimitRow.key == key,
                    expired | (RateLimitRow.attempts < limit)).values(
                        attempts=case((expired, 1), else_=RateLimitRow.attempts + 1),
                        window_start=case((expired, now), else_=RateLimitRow.window_start)))
                if changed.rowcount != 1:
                    raise RateLimited()
            session.execute(delete(RateLimitRow).where(RateLimitRow.window_start < now - timedelta(days=1)))
            session.execute(delete(SessionRow).where(SessionRow.expires_at <= now))
