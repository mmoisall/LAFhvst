/* 원격 접속 설정 패널(frontend/js/remote.js) 동작 검증 — DOM/API 스텁으로 Node 에서 실행.
 *
 * 검증: ① 정상 상태 렌더(카드 텍스트/QR/기기 목록) ② 요청이 영구 대기하면 타임아웃 메시지로 바뀐다
 *       (증상: "상태 확인 중…" 에서 멈춤) ③ 켜기 버튼이 진행 상황/실패를 표시한다.
 *
 * 실행: node tests/ui_remote_panel.test.js
 */

const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..");
const failures = [];
let passed = 0;

function ok(condition, message) {
  if (condition) {
    passed += 1;
    console.log("PASS " + message);
  } else {
    failures.push(message);
    console.log("FAIL " + message);
  }
}

// --- DOM 스텁 --------------------------------------------------------------
function makeElement(id) {
  return {
    id: id,
    textContent: "",
    value: "",
    src: "",
    innerHTML: "",
    checked: false,
    classList: {
      _set: new Set(),
      add(name) {
        this._set.add(name);
      },
      remove(name) {
        this._set.delete(name);
      },
      toggle(name, on) {
        if (on) {
          this._set.add(name);
        } else {
          this._set.delete(name);
        }
      },
      contains(name) {
        return this._set.has(name);
      }
    },
    addEventListener() {},
    removeAttribute(name) {
      if (name === "src") {
        this.src = "";
      }
    },
    querySelectorAll() {
      return [];
    },
    getAttribute() {
      return "";
    }
  };
}

const elements = {};
function byId(id) {
  if (!elements[id]) {
    elements[id] = makeElement(id);
  }
  return elements[id];
}

const timers = [];
globalThis.document = {
  body: makeElement("body"),
  getElementById: (id) => (elements[id] === undefined ? null : elements[id]),
  querySelectorAll: () => [],
  activeElement: null
};
globalThis.window = globalThis;
globalThis.setTimeout = (fn, ms) => {
  timers.push({ fn: fn, ms: ms });
  return timers.length;
};
globalThis.clearTimeout = () => {};
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};
globalThis.confirm = () => true;
globalThis.Toast = { show: () => {} };

// 필요한 요소를 미리 만들어 둔다(remote.js 는 byId 로만 접근).
["remoteStateText", "remoteToggleBtn", "remoteRotateBtn", "remoteCopyLinkBtn",
 "remoteQrImg", "remoteLinkText", "remoteUrlText", "remoteRelayUrlInput",
 "remoteDeviceList", "remoteLocalActions", "remotePairMeta", "view-settings"].forEach(byId);

// --- API 스텁 --------------------------------------------------------------
let mode = "ok";
let enableCalled = 0;
const pending = () => new Promise(() => {}); // 영구 대기(차단/프록시 시나리오)
globalThis.API = {
  remoteStatus: () =>
    mode === "hang"
      ? pending()
      : Promise.resolve({
          enabled: false,
          running: false,
          transport: "none",
          url: "",
          error: "",
          relay_url: "",
          host_id: "HOSTID1234567890",
          allow_destructive: false,
          device: null,
          devices: [],
          pair: null
        }),
  remoteEnable: () => {
    enableCalled += 1;
    return mode === "hang" ? pending() : Promise.resolve({ ok: true, pair: null, state: {} });
  },
  remoteDisable: () => (mode === "hang" ? pending() : Promise.resolve({ ok: true })),
  remoteRotate: () => (mode === "hang" ? pending() : Promise.resolve({ ok: true, pair: null })),
  remoteRevokeDevice: () => Promise.resolve({ ok: true }),
  saveSettings: () => (mode === "hang" ? pending() : Promise.resolve({})),
  remoteQrUrl: () => "http://127.0.0.1/qr.png"
};

require(path.join(REPO, "frontend", "js", "remote.js"));

function flushTimers() {
  const due = timers.splice(0, timers.length);
  due.forEach((entry) => entry.fn());
}

async function main() {
  ok(typeof globalThis.RemotePanel === "function" || typeof globalThis.RemotePanel === "object",
     "RemotePanel 노출");

  // ① 정상 상태
  await globalThis.RemotePanel.refresh();
  ok(byId("remoteStateText").textContent.indexOf("꺼져") >= 0,
     "상태 텍스트 렌더(꺼짐): " + byId("remoteStateText").textContent);
  ok(byId("remoteToggleBtn").textContent.indexOf("켜기") >= 0,
     "토글 버튼 라벨: " + byId("remoteToggleBtn").textContent);

  // ② 요청이 영구 대기 → 타임아웃 메시지(무한 "상태 확인 중…" 방지)
  mode = "hang";
  const pendingRefresh = globalThis.RemotePanel.refresh();
  flushTimers(); // 8초 타임아웃 발동
  await pendingRefresh;
  ok(byId("remoteStateText").textContent.indexOf("응답 없음") >= 0,
     "타임아웃 시 이유 표시: " + byId("remoteStateText").textContent);

  // ③ 켜기 버튼: 진행 표시 후 실패 이유 표시
  byId("remoteStateText").textContent = "";
  elements.remoteToggleBtn.addEventListener = function (name, handler) {
    this._handler = handler;
  };
  globalThis.RemotePanel.init();
  ok(typeof elements.remoteToggleBtn._handler === "function", "토글 버튼 핸들러 바인딩");
  elements.remoteToggleBtn._handler();
  ok(byId("remoteStateText").textContent.indexOf("켜는 중") >= 0,
     "진행 상황 표시: " + byId("remoteStateText").textContent);
  flushTimers();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  ok(byId("remoteStateText").textContent.indexOf("응답 없음") >= 0,
     "실패 이유 표시: " + byId("remoteStateText").textContent);

  // ④ 정상 경로: 저장 → 켜기 → 상태 재조회가 순서대로 호출된다.
  mode = "ok";
  elements.remoteToggleBtn._handler();
  for (let i = 0; i < 6; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  ok(enableCalled >= 1, "정상 경로에서 enable 호출됨: " + enableCalled);
  ok(byId("remoteStateText").textContent.indexOf("꺼져") >= 0,
     "재조회로 상태 텍스트 갱신: " + byId("remoteStateText").textContent);

  console.log("FAILURES: " + (failures.length ? failures.join(", ") : "none") + " (" + passed + " passed)");
  process.exitCode = failures.length ? 1 : 0;
}

main().catch((error) => {
  console.error("오류:", error && error.message ? error.message : error);
  process.exitCode = 1;
});
