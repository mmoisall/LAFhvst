"""PWA 자산 서빙 테스트(M7).

매니페스트가 참조하는 아이콘 파일이 실제로 서빙되는지까지 확인한다
(존재하지 않는 아이콘 참조는 설치 실패로 이어지는 실제 버그).
"""

import json

from conftest import enable_remote

MANIFEST = "/manifest.webmanifest"
SW = "/sw.js"


def test_pwa_assets_are_served_without_token(local_client, remote_client):
    enable_remote(local_client)
    for path, content_type in (
        (MANIFEST, "application/manifest+json"),
        (SW, "text/javascript"),
        ("/icons/icon-192.png", "image/png"),
    ):
        local = local_client.get(path)
        assert local.status_code == 200, path
        assert content_type in local.headers["content-type"], (path, local.headers)
        # 공개 자산이라 원격에서도 토큰 없이 접근할 수 있어야 한다.
        remote = remote_client.get(path)
        assert remote.status_code == 200, path
        assert remote.content == local.content, path


def test_manifest_icons_exist(local_client):
    manifest = json.loads(local_client.get(MANIFEST).content.decode("utf-8"))
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["icons"], "아이콘 정의가 없습니다"
    for icon in manifest["icons"]:
        response = local_client.get("/" + icon["src"].lstrip("/"))
        assert response.status_code == 200, icon["src"]
        assert response.headers["content-type"] == "image/png"
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n", icon["src"]


def test_shell_assets_are_revalidated(local_client):
    """앱 셸(HTML/JS/CSS)은 재검증 헤더를 보내야 한다.

    헤더가 없으면 브라우저 휴리스틱 캐시가 **예전 JS** 를 재사용해 "UI 는 새것인데
    스크립트는 예전 것"(설정 카드가 초기화되지 않음) 상태가 된다.
    """
    for path in ("/", "/index.html", "/pair.html", "/js/app.js", "/js/remote.js", "/css/style.css"):
        response = local_client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "no-cache", (path, response.headers)


def test_service_worker_served_for_root_scope(local_client):
    response = local_client.get(SW)
    assert response.status_code == 200
    # 루트에서 서빙되어야 전체 경로를 제어할 수 있다(스코프).
    assert "addEventListener" in response.text
