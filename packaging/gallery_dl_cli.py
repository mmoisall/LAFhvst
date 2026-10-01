"""PyInstaller용 gallery-dl 콘솔 진입점.

`gallery-dl.exe` 로 빌드되어 LAFhvst.exe 옆에 배치된다.
core/gdl_executor.gallery_dl_path() 가 실행 파일 폴더에서 이를 찾아 실행한다.
"""

from gallery_dl import main

if __name__ == "__main__":
    raise SystemExit(main())
