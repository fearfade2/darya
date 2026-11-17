import hashlib
import hmac
import secrets
import time


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex()
    return salt + ":" + digest


def password_matches(password, encoded):
    try:
        salt, digest = encoded.split(":")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310000).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, AttributeError):
        return False


def session_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(store):
    token = secrets.token_urlsafe(40)
    store.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
    store.execute("INSERT INTO sessions VALUES (?,?)", (session_hash(token), time.time() + 7 * 86400))
    return token


def valid_session(store, token):
    return bool(token and store.rows("SELECT 1 FROM sessions WHERE token_hash=? AND expires>?", (session_hash(token), time.time())))
