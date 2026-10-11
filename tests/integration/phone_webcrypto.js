/* Node '폰' 검사 — 브라우저와 같은 WebCrypto 구현(`frontend/js/e2e.js`)으로 릴레이를 경유해 앱을 제어한다.
 *
 * 브라우저 없이 E2E 경로를 검증한다. Node 20+ (`crypto.webcrypto`, 전역 `fetch`) 필요.
 *
 * 사용:
 *   node tests/integration/phone_webcrypto.js                       # 기본(127.0.0.1:17470 / :8788)
 *   node tests/integration/phone_webcrypto.js http://127.0.0.1:17599   # 패키징 exe 등 다른 포트
 *   node tests/integration/phone_webcrypto.js <app> <relay>
 *
 * 전제: 앱이 원격 접속 켜짐 + 릴레이 주소 설정 상태로 실행 중이어야 한다.
 *       (예: `python tests/integration/local_relay.py --keep`)
 *
 * 검증 항목: v2 암호화 페어링, 세션 봉투 왕복, HTTP 레벨 Authorization 헤더 부재,
 *            봉투 평문 비노출, 스코프(파괴적 작업 거부), 재전송 거부, 새로고침 후 카운터 지속, v1 Bearer 호환.
 */

const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..", "..");
const APP = process.argv[2] || "http://127.0.0.1:17470";
const RELAY = process.argv[3] || "http://127.0.0.1:8788";

// --- 브라우저 환경 스텁 -----------------------------------------------------
const store = {};
globalThis.localStorage = {
  getItem: (key) => (Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null),
  setItem: (key, value) => {
    store[key] = String(value);
  },
  removeItem: (key) => {
    delete store[key];
  }
};
globalThis.crypto = require("crypto").webcrypto;
globalThis.window = globalThis;

// 상대 경로(/api/e2e)를 릴레이 공개 주소로 바꾼다(브라우저에서는 자동).
let prefix = "";
let publicBase = RELAY;
const realFetch = globalThis.fetch;
let lastSent = { url: "", headers: [], bodyPreview: "" };
globalThis.fetch = (url, options) => {
  const target = String(url).startsWith("http") ? String(url) : publicBase + url;
  const opts = options || {};
  if (String(url).startsWith("/api/e2e")) {
    lastSent = {
      url: target,
      headers: Object.keys(opts.headers || {}),
      bodyPreview: Buffer.from(opts.body || []).slice(0, 24).toString("hex")
    };
  }
  return realFetch(target, opts);
};

require(path.join(REPO, "frontend", "js", "e2e.js"));
const E2E = globalThis.E2E;

const failures = [];
function check(label, actual, expected) {
  const ok = actual === expected;
  console.log((ok ? "PASS " : "FAIL ") + label + ": " + JSON.stringify(actual));
  if (!ok) {
    failures.push(label);
  }
}
function checkTrue(label, value) {
  check(label, Boolean(value), true);
}

async function main() {
  checkTrue("E2E 모듈 로드", E2E && typeof E2E.request === "function");
  checkTrue("WebCrypto 사용 가능", E2E.supported());

  const statusResponse = await realFetch(APP + "/api/remote/status");
  const status = await statusResponse.json();
  checkTrue("릴레이 연결됨", status.running);
  prefix = "/h/" + status.host_id;
  publicBase = RELAY + prefix;

  const pair = status.pair;
  checkTrue("페어링 링크에 앱 공개키 포함", pair && pair.link.includes("pk="));
  const token = decodeURIComponent(pair.link.split("#t=")[1].split("&")[0]);
  const appPublic = pair.app_public_key;

  // v2 페어링(릴레이 경유) — HTTP 레벨에는 기기 토큰이 없다.
  const identity = await E2E.ensureIdentity();
  const pairKey = await E2E.pairKeyFor(appPublic, token);
  const enc = await E2E.sealPair(pairKey, { t: token, name: "Node 폰" }, "lafhvst-pair-v2|req|");
  const claimResponse = await realFetch(RELAY + prefix + "/api/pair/claim", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ v: 2, pk: identity.publicB64, enc: enc })
  });
  check("claim 상태", claimResponse.status, 200);
  const claimBody = await claimResponse.json();
  const inner = await E2E.openPair(pairKey, claimBody.enc, "lafhvst-pair-v2|res|");
  checkTrue("device_token 수신", typeof inner.device_token === "string" && inner.device_token.length > 20);
  checkTrue("device_id 수신", inner.device_id > 0);
  checkTrue("claim 요청 본문에 토큰 평문 없음", !JSON.stringify({ enc: enc }).includes(token));

  // 세션 봉투로 API 호출.
  E2E.configure({ deviceId: inner.device_id, deviceToken: inner.device_token, appPublicB64: appPublic });

  const settings = await E2E.request({ method: "GET", path: "/api/settings" });
  check("GET /api/settings 상태", settings.status, 200);
  checkTrue("설정 본문 수신", JSON.parse(new TextDecoder().decode(settings.body)).remoteEnabled === true);
  checkTrue(
    "HTTP 레벨 Authorization 헤더 없음",
    !lastSent.headers.some((name) => name.toLowerCase() === "authorization")
  );
  check("봉투 경로", lastSent.url, RELAY + prefix + "/api/e2e");
  checkTrue("봉투 본문은 매직 0x02 로 시작", lastSent.bodyPreview.startsWith("02"));
  checkTrue("봉투에 토큰 평문 없음", !JSON.stringify(lastSent).includes(inner.device_token));

  const version = await E2E.request({ method: "GET", path: "/api/version" });
  check("버전 응답", version.status, 200);
  checkTrue("버전 값", typeof JSON.parse(new TextDecoder().decode(version.body)).version === "string");

  const live = await E2E.request({ method: "GET", path: "/api/dashboard/live" });
  check("대시보드 상태", live.status, 200);

  const post = await E2E.request({
    method: "POST",
    path: "/api/quick-download",
    headers: { "content-type": "application/json" },
    body: new TextEncoder().encode(JSON.stringify({ url: "" }))
  });
  checkTrue("POST 봉투 처리(422 아님)", post.status !== 422);

  const denied = await E2E.request({
    method: "POST",
    path: "/api/explorer/batch-delete",
    headers: { "content-type": "application/json" },
    body: new TextEncoder().encode(JSON.stringify({ items: [] }))
  });
  check("파괴적 작업 거부(옵트인 전)", denied.status, 403);

  // 재전송 방지: 같은 봉투 두 번 → 두 번째 401.
  const replay = await E2E.encodeRequest({ method: "GET", path: "/api/version" });
  const first = await realFetch(RELAY + prefix + "/api/e2e", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: replay
  });
  const second = await realFetch(RELAY + prefix + "/api/e2e", {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: replay
  });
  check("첫 봉투", first.status, 200);
  check("재전송 거부", second.status, 401);

  // 카운터 지속(새로고침 시뮬레이션).
  E2E.configure({ deviceId: inner.device_id, deviceToken: inner.device_token, appPublicB64: appPublic });
  const afterReload = await E2E.request({ method: "GET", path: "/api/version" });
  check("새로고침 후 요청", afterReload.status, 200);

  // v1(평문) Bearer 경로도 유지되어야 한다(하위 호환).
  const v1 = await realFetch(RELAY + prefix + "/api/settings", {
    headers: { Authorization: "Bearer " + inner.device_token }
  });
  check("v1 Bearer 경로 유지", v1.status, 200);

  console.log("FAILURES: " + (failures.length ? failures.join(", ") : "none"));
  process.exitCode = failures.length ? 1 : 0;
}

main().catch((error) => {
  console.error("오류:", error && error.message ? error.message : error);
  process.exitCode = 1;
});
