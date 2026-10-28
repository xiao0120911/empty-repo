"""账号密码本地校验与CSRF辅助；mock平台任意账号不能获得本应用权限。"""
import hashlib
import hmac
import secrets


COMMON_PASSWORDS = {'password', 'password123', 'admin123', '123456', '12345678', 'qwerty123'}


def password_policy_error(password):
    """Return a user-safe error for credentials that do not meet the local policy."""
    if len(password) < 12:
        return '密码至少需要 12 位'
    if password.casefold() in COMMON_PASSWORDS:
        return '密码过于常见'
    categories = sum((
        any(char.islower() for char in password),
        any(char.isupper() for char in password),
        any(char.isdigit() for char in password),
        any(not char.isalnum() for char in password),
    ))
    if categories < 3:
        return '密码须至少包含大写、小写、数字、符号中的三类字符'
    return None

def password_hash(password):
    error = password_policy_error(password)
    if error:
        raise ValueError(error)
    salt=secrets.token_bytes(16)
    value=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1)
    return 'scrypt$'+salt.hex()+'$'+value.hex()

def password_check(password,encoded):
    try:
        algorithm,salt,value=encoded.split('$')
        if algorithm!='scrypt':
            return False
        actual=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
        return hmac.compare_digest(actual,value)
    except (ValueError,TypeError):
        return False
