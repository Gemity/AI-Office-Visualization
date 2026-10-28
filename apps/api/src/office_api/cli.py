"""office-api CLI.

office-api create-owner --name "Quy"   # prints a bearer token once
office-api serve [--host 127.0.0.1] [--port 8000]
"""

import argparse
import asyncio

from office_api.auth import create_principal_with_token
from office_api.config import Settings
from office_api.db import Base, create_engine, create_sessionmaker


async def _create_owner(name: str) -> None:
    engine = create_engine(Settings().database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with create_sessionmaker(engine)() as session:
        principal, token = await create_principal_with_token(session, name)
    await engine.dispose()
    print(f"principal_id: {principal.id}")
    print(f"token:        {token}")
    print("Store the token now; only its hash is kept.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="office-api")
    sub = parser.add_subparsers(dest="cmd", required=True)
    owner = sub.add_parser("create-owner", help="create a principal and print its API token")
    owner.add_argument("--name", required=True)
    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.cmd == "create-owner":
        asyncio.run(_create_owner(args.name))
    elif args.cmd == "serve":
        import uvicorn

        from office_api.main import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
