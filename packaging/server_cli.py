"""PyInstaller용 서버(콘솔) 진입점.

창 없이 서버만 실행한다(`LAF_NO_GUI=1`). 콘솔 창에 배너/로그가 표시되며
`Ctrl+C` 로 종료할 수 있다. 포트는 `--port N` 또는 `LAF_PORT` 환경변수로 지정.
"""

import multiprocessing
import os
import sys

os.environ["LAF_NO_GUI"] = "1"

from main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
