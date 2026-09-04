import argparse
import getpass
import sys
from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Session, select

from app.db.session import create_db_and_tables, engine
from app.models.user import User
from app.core.security import hash_password, validate_new_password


def create_admin_cmd(args, session: Optional[Session] = None):
    """Create or promote an administrator user."""
    create_db_and_tables()

    username = args.username
    if not username:
        username = input("Introduce el nombre de usuario del administrador: ").strip()

    if len(username) < 3:
        print("Error: El nombre de usuario debe tener al menos 3 caracteres.", file=sys.stderr)
        sys.exit(1)

    password = args.password
    if not password:
        password = getpass.getpass("Introduce la contraseña del administrador: ")
        password_confirm = getpass.getpass("Confirma la contraseña: ")
        if password != password_confirm:
            print("Error: Las contraseñas no coinciden.", file=sys.stderr)
            sys.exit(1)

    try:
        validate_new_password(password)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    def _execute(s: Session):
        existing = s.exec(select(User).where(User.username == username)).first()
        if existing:
            existing.password_hash = hash_password(password)
            existing.role = "admin"
            existing.is_active = True
            existing.must_change_password = False
            s.add(existing)
            s.commit()
            print(f"El usuario existente '{username}' ha sido actualizado a Administrador.")
        else:
            admin_user = User(
                username=username,
                password_hash=hash_password(password),
                role="admin",
                is_active=True,
                created_at=datetime.now(timezone.utc),
            )
            s.add(admin_user)
            s.commit()
            print(f"Administrador '{username}' creado exitosamente.")

    if session:
        _execute(session)
    else:
        with Session(engine) as s:
            _execute(s)


def main():
    parser = argparse.ArgumentParser(description="SimpliTV CLI Management")
    subparsers = parser.add_subparsers(dest="command", help="Comandos disponibles")

    # create-admin
    admin_parser = subparsers.add_parser("create-admin", help="Crear un usuario administrador")
    admin_parser.add_argument("--username", "-u", type=str, help="Nombre de usuario del administrador")
    admin_parser.add_argument("--password", "-p", type=str, help="Contraseña del administrador")

    args = parser.parse_args()

    if args.command == "create-admin":
        create_admin_cmd(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
