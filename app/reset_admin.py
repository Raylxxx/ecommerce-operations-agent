"""把 admin 的登录密码重置为 .env 里的 ADMIN_PASSWORD。

python -m app.reset_admin
"""
from sqlalchemy import select

from . import config
from .main import hash_password
from .models import SessionLocal, User

if __name__ == "__main__":
    if not config.ADMIN_PASSWORD:
        raise SystemExit(".env 中没有设置 ADMIN_PASSWORD")
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        if not admin:
            raise SystemExit("找不到 admin 账号")
        admin.password_hash = hash_password(config.ADMIN_PASSWORD)
        admin.is_active = True
        db.commit()
    print("admin 密码已更新，请用 .env 中的 ADMIN_PASSWORD 登录")
