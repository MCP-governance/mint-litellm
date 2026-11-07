"""``mint-litellm console``: 필수 설정을 확인하고 uvicorn으로 콘솔 앱을 띄운다."""

import argparse
import sys
from collections.abc import Mapping

from mint_litellm.console.app import create_app
from mint_litellm.console.core import ConsoleConfig


def serve(args: argparse.Namespace, environ: Mapping[str, str]) -> int:
    import uvicorn

    # 필수 설정이 없으면 ControlError로 기동을 거부한다
    app = create_app(ConsoleConfig.from_env(environ))
    print(f"관리자 콘솔 시작: {args.host}:{args.port}", file=sys.stderr, flush=True)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        log_level="warning",
    )
    return 0
