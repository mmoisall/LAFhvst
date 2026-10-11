/* E2E v2 교차 검증 테스트 — `node tests/e2e_vectors.test.js`.
   파이썬(core/remote_crypto.py)이 만든 고정 벡터를 JS(frontend/js/e2e.js)가 복호화하는지,
   자체 왕복과 음성 케이스가 동작하는지 확인한다. 외부 네트워크를 쓰지 않는다. */
"use strict";

var fs = require("fs");
var path = require("path");
var vm = require("vm");
var nodeCrypto = require("crypto");

if (!globalThis.crypto || !globalThis.crypto.subtle) {
  globalThis.crypto = nodeCrypto.webcrypto;
}

// 브라우저 localStorage 대체(개인키 JWK 보관용).
var store = {};
globalThis.localStorage = {
  getItem: function (key) {
    return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
  },
  setItem: function (key, value) {
    store[key] = String(value);
  },
  removeItem: function (key) {
    delete store[key];
  }
};

var ROOT = path.join(__dirname, "..");
var vectors = JSON.parse(fs.readFileSync(path.join(__dirname, "vectors", "e2e_v2.json"), "utf8"));

vm.runInThisContext(fs.readFileSync(path.join(ROOT, "frontend", "js", "e2e.js"), "utf8"), {
  filename: "frontend/js/e2e.js"
});
var E2E = globalThis.E2E;

var subtle = globalThis.crypto.subtle;
var encoder = new TextEncoder();
var decoder = new TextDecoder();

var AAD_PAIR_REQUEST = "lafhvst-pair-v2|req|";
var AAD_PAIR_RESPONSE = "lafhvst-pair-v2|res|";

var passed = 0;

function ok(condition, message) {
  if (!condition) {
    throw new Error("단언 실패: " + message);
  }
  passed++;
}

function fail(message) {
  throw new Error("단언 실패: " + message);
}

async function expectThrows(fn, message) {
  var threw = false;
  try {
    await fn();
  } catch (error) {
    threw = true;
  }
  ok(threw, message);
}

function utf8(text) {
  return encoder.encode(String(text));
}

function b64urlDecode(text) {
  var value = String(text || "").replace(/-/g, "+").replace(/_/g, "/");
  var remainder = value.length % 4;
  if (remainder === 2) {
    value += "==";
  } else if (remainder === 3) {
    value += "=";
  }
  return new Uint8Array(Buffer.from(value, "base64"));
}

function toHex(bytes) {
  return Buffer.from(bytes).toString("hex");
}

function fromHex(text) {
  return new Uint8Array(Buffer.from(String(text), "hex"));
}

// 벡터 JSON 은 키가 정렬되어 있어 순서 비교는 무의미하다(내용만 본다).
function sameHeaders(actual, expected) {
  function canonical(value) {
    var out = {};
    Object.keys(value || {})
      .sort()
      .forEach(function (key) {
        out[key] = value[key];
      });
    return JSON.stringify(out);
  }
  return canonical(actual) === canonical(expected);
}

// --- 파이썬과 독립적으로 유도하는 저수준 헬퍼 --------------------------------
async function importPkcs8(b64) {
  return subtle.importKey("pkcs8", b64urlDecode(b64), { name: "ECDH", namedCurve: "P-256" }, true, [
    "deriveBits"
  ]);
}

async function importSpki(b64) {
  return subtle.importKey("spki", b64urlDecode(b64), { name: "ECDH", namedCurve: "P-256" }, true, []);
}

async function ecdhBits(privateKey, peerPublicB64) {
  var peer = await importSpki(peerPublicB64);
  var shared = await subtle.deriveBits({ name: "ECDH", public: peer }, privateKey, 256);
  return new Uint8Array(shared);
}

async function hkdfBits(ikm, saltBytes, infoBytes) {
  var key = await subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  var out = await subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: saltBytes, info: infoBytes },
    key,
    256
  );
  return new Uint8Array(out);
}

async function aesDecrypt(keyBytes, nonce, aadBytes, ciphertext) {
  var key = await subtle.importKey("raw", keyBytes, { name: "AES-GCM" }, false, ["decrypt"]);
  var plaintext = await subtle.decrypt(
    { name: "AES-GCM", iv: nonce, additionalData: aadBytes },
    key,
    ciphertext
  );
  return new Uint8Array(plaintext);
}

async function aesEncrypt(keyBytes, nonce, aadBytes, plaintext) {
  var key = await subtle.importKey("raw", keyBytes, { name: "AES-GCM" }, false, ["encrypt"]);
  var ciphertext = await subtle.encrypt(
    { name: "AES-GCM", iv: nonce, additionalData: aadBytes },
    key,
    plaintext
  );
  return new Uint8Array(ciphertext);
}

// 파이썬이 만든 요청 봉투를 세션 키로 직접 푼다(decodeResponse 와 별개의 경로).
async function parseRequestEnvelope(sessionKeyBytes, envelope) {
  var raw = envelope instanceof Uint8Array ? envelope : new Uint8Array(envelope);
  if (raw[0] !== 0x02) {
    fail("요청 봉투 매직");
  }
  var deviceId = new DataView(raw.buffer, raw.byteOffset + 1, 4).getUint32(0);
  var nonce = raw.subarray(5, 17);
  if (nonce[8] !== 0) {
    fail("요청 봉투 방향");
  }
  var counter = Number(new DataView(raw.buffer, raw.byteOffset + 5, 8).getBigUint64(0));
  var aad = utf8("lafhvst-e2e-v1|req|" + deviceId);
  var plaintext = await aesDecrypt(sessionKeyBytes, nonce, aad, raw.subarray(17));
  return { deviceId: deviceId, counter: counter, data: JSON.parse(decoder.decode(plaintext)) };
}

async function main() {
  ok(E2E && typeof E2E === "object", "E2E 노출");
  ok(E2E.supported() === true, "supported()");

  // 폰 개인키(파이썬 벡터)를 JWK 로 바꿔 localStorage 에 심는다.
  var phonePrivate = await importPkcs8(vectors.phone.private);
  var phoneJwk = await subtle.exportKey("jwk", phonePrivate);
  store.lafE2EPrivateJwk = JSON.stringify(phoneJwk);
  store.lafE2EPublicB64 = vectors.phone.public;

  var identity = await E2E.ensureIdentity();
  ok(identity.publicB64 === vectors.phone.public, "ensureIdentity 가 벡터 폰 공개키를 복원");
  ok(E2E.identity().publicB64 === vectors.phone.public, "identity() 공개키");
  ok(E2E.identity().privateJwk && E2E.identity().privateJwk.d, "identity() 개인 JWK");

  // --- 1. JS 가 스스로 유도한 키가 벡터와 일치 -----------------------------
  var shared = await ecdhBits(phonePrivate, vectors.app.public);
  var sessionKeyBytes = await hkdfBits(shared, utf8("lafhvst-e2e-v1"), utf8("device:" + vectors.device_id));
  ok(toHex(sessionKeyBytes) === vectors.session_key_hex, "session_key 유도 일치");

  var pairKeyBytes = await hkdfBits(shared, utf8(vectors.pair_token), utf8("lafhvst-pair-v2"));
  ok(toHex(pairKeyBytes) === vectors.pair_key_hex, "pair_key 유도 일치");

  // --- 1a. 파이썬 요청 봉투 복호화 -----------------------------------------
  var request = await parseRequestEnvelope(sessionKeyBytes, b64urlDecode(vectors.request.envelope));
  ok(request.counter === vectors.request.counter, "요청 카운터 일치");
  ok(request.deviceId === vectors.device_id, "요청 기기 id 일치");
  ok(request.data.path === vectors.request.plaintext.path, "요청 path 일치");
  ok(request.data.method === vectors.request.plaintext.method, "요청 method 일치");
  ok(sameHeaders(request.data.headers, vectors.request.plaintext.headers), "요청 헤더 일치");

  // --- 1b. 파이썬 응답 봉투 (모듈 decodeResponse) --------------------------
  E2E.configure({
    deviceId: vectors.device_id,
    deviceToken: vectors.request.plaintext.headers.authorization.replace("Bearer ", ""),
    appPublicB64: vectors.app.public
  });
  ok(E2E.configured() === true, "configured()");
  var response = await E2E.decodeResponse(fromHex(vectors.response.envelope_hex));
  ok(response.status === vectors.response.plaintext.status, "응답 status 일치");
  ok(sameHeaders(response.headers, vectors.response.plaintext.headers), "응답 헤더 일치");
  ok(
    decoder.decode(response.body) === decoder.decode(b64urlDecode(vectors.response.plaintext.body_b64)),
    "응답 body 일치"
  );

  // --- 1c. 페어링 봉투 ------------------------------------------------------
  var pairKey = await E2E.pairKeyFor(vectors.app.public, vectors.pair_token);
  ok(pairKey instanceof CryptoKey, "pairKeyFor 가 CryptoKey 반환");
  var pairRequest = await E2E.openPair(pairKey, vectors.pair_request.enc, AAD_PAIR_REQUEST);
  ok(pairRequest.t === vectors.pair_request.plaintext.t, "페어링 요청 t 일치");
  ok(pairRequest.name === vectors.pair_request.plaintext.name, "페어링 요청 name 일치");
  var pairResponse = await E2E.openPair(pairKey, vectors.pair_response.enc, AAD_PAIR_RESPONSE);
  ok(pairResponse.device_id === vectors.pair_response.plaintext.device_id, "페어링 응답 device_id");
  ok(pairResponse.device_token === vectors.pair_response.plaintext.device_token, "페어링 응답 device_token");
  ok(pairResponse.host_id === vectors.pair_response.plaintext.host_id, "페어링 응답 host_id");
  ok(pairResponse.public_url === vectors.pair_response.plaintext.public_url, "페어링 응답 public_url");
  ok(vectors.pair_request.pk === vectors.phone.public, "벡터 pk = 폰 공개키");

  // --- 2. 자체 왕복: sealPair / openPair -----------------------------------
  var selfKey = await E2E.pairKeyFor(vectors.app.public, "SELF-TOKEN");
  var sealed = await E2E.sealPair(selfKey, { a: 1, b: "한글", nested: { c: true } }, AAD_PAIR_REQUEST);
  ok(typeof sealed === "string" && !/=/.test(sealed), "sealPair base64url(무패딩)");
  var opened = await E2E.openPair(selfKey, sealed, AAD_PAIR_REQUEST);
  ok(opened.a === 1 && opened.b === "한글" && opened.nested.c === true, "sealPair/openPair 왕복");

  // --- 2b. 자체 왕복: encodeRequest (모듈) → 독립 복호화 --------------------
  E2E.configure({
    deviceId: vectors.device_id,
    deviceToken: "DEVICE-TOKEN",
    appPublicB64: vectors.app.public
  });
  E2E.clearCounter(); // 카운터 시작을 0 으로 고정(이후 증가 검증)
  var body = utf8("안녕하세요");
  var envelope1 = await E2E.encodeRequest({
    method: "POST",
    path: "/api/x",
    query: "a=1&b=2",
    headers: { accept: "application/json" },
    body: body
  });
  var parsed1 = await parseRequestEnvelope(sessionKeyBytes, envelope1);
  ok(parsed1.counter === 0, "요청 카운터 0부터 시작");
  ok(parsed1.data.method === "POST" && parsed1.data.path === "/api/x", "요청 method/path 필드");
  ok(parsed1.data.query === "a=1&b=2", "요청 query 필드");
  ok(parsed1.data.headers.accept === "application/json", "요청 헤더 필드");
  ok(decoder.decode(b64urlDecode(parsed1.data.body_b64)) === "안녕하세요", "요청 본문 왕복");

  var envelope2 = await E2E.encodeRequest({ method: "GET", path: "/api/y" });
  var parsed2 = await parseRequestEnvelope(sessionKeyBytes, envelope2);
  ok(parsed2.counter === 1, "요청 카운터 증가(재사용 금지)");

  // --- 2c. request() 왕복(fetch 스텁) ---------------------------------------
  var stubResponseNonce = new Uint8Array(12);
  new DataView(stubResponseNonce.buffer).setBigUint64(0, 0n);
  stubResponseNonce[8] = 1;
  var stubPlaintext = encoder.encode(
    JSON.stringify({
      status: 201,
      headers: { "content-type": "application/json" },
      body_b64: "aGk"
    })
  );
  var stubCiphertext = await aesEncrypt(
    sessionKeyBytes,
    stubResponseNonce,
    utf8("lafhvst-e2e-v1|res|" + vectors.device_id),
    stubPlaintext
  );
  var stubEnvelope = new Uint8Array(1 + 12 + stubCiphertext.length);
  stubEnvelope[0] = 0x02;
  stubEnvelope.set(stubResponseNonce, 1);
  stubEnvelope.set(stubCiphertext, 13);

  E2E.configure({
    deviceId: vectors.device_id,
    deviceToken: "DEVICE-TOKEN",
    appPublicB64: vectors.app.public
  });
  var captured = null;
  var realFetch = globalThis.fetch;
  globalThis.fetch = function (url, options) {
    captured = { url: url, options: options };
    return Promise.resolve({
      status: 200,
      ok: true,
      arrayBuffer: function () {
        return Promise.resolve(stubEnvelope.buffer.slice(0));
      }
    });
  };
  try {
    var result = await E2E.request({ method: "GET", path: "/api/version", headers: { accept: "application/json" } });
    ok(captured.url === "/api/e2e", "request() 가 POST /api/e2e 로 전송");
    ok(captured.options.method === "POST", "request() HTTP 메서드");
    ok(captured.options.headers["Content-Type"] === "application/octet-stream", "request() Content-Type");
    ok(captured.options.body instanceof Uint8Array, "request() 본문이 봉투 바이트");
    var sent = await parseRequestEnvelope(sessionKeyBytes, captured.options.body);
    ok(sent.data.headers.authorization === "Bearer DEVICE-TOKEN", "요청 헤더에 기기 토큰 자동 주입");
    ok(result.status === 201 && decoder.decode(result.body) === "hi", "request() 응답 복호화");
  } finally {
    globalThis.fetch = realFetch;
  }

  // --- 3. 음성 케이스 -------------------------------------------------------
  E2E.configure({
    deviceId: vectors.device_id,
    deviceToken: "DEVICE-TOKEN",
    appPublicB64: vectors.app.public
  });

  var tampered = fromHex(vectors.response.envelope_hex);
  tampered[tampered.length - 1] ^= 0x01;
  await expectThrows(function () {
    return E2E.decodeResponse(tampered);
  }, "변조된 응답 봉투 거부");

  var badMagic = fromHex(vectors.response.envelope_hex);
  badMagic[0] = 0x03;
  await expectThrows(function () {
    return E2E.decodeResponse(badMagic);
  }, "잘못된 매직 거부");

  await expectThrows(function () {
    return E2E.decodeResponse(b64urlDecode(vectors.request.envelope));
  }, "요청 방향 봉투를 응답으로 거부");

  await expectThrows(function () {
    return E2E.decodeResponse(fromHex(vectors.response.envelope_hex).subarray(0, 10));
  }, "너무 짧은 봉투 거부");

  E2E.configure({ deviceId: 8, deviceToken: "DEVICE-TOKEN", appPublicB64: vectors.app.public });
  await expectThrows(function () {
    return E2E.decodeResponse(fromHex(vectors.response.envelope_hex));
  }, "다른 기기 id(AAD 불일치) 거부");

  E2E.reset();
  ok(E2E.configured() === false, "reset() 후 configured() false");
  await expectThrows(function () {
    return E2E.encodeRequest({ method: "GET", path: "/" });
  }, "미설정 상태 encodeRequest 거부");

  var wrongKey = await E2E.pairKeyFor(vectors.app.public, "WRONG-TOKEN");
  await expectThrows(function () {
    return E2E.openPair(wrongKey, vectors.pair_response.enc, AAD_PAIR_RESPONSE);
  }, "다른 키로 페어링 봉투 거부");

  await expectThrows(function () {
    return E2E.openPair(pairKey, vectors.pair_request.enc, AAD_PAIR_RESPONSE);
  }, "잘못된 AAD 로 페어링 봉투 거부");

  var tamperedPair = vectors.pair_request.enc.slice(0, 20) + (vectors.pair_request.enc[20] === "A" ? "B" : "A") + vectors.pair_request.enc.slice(21);
  await expectThrows(function () {
    return E2E.openPair(pairKey, tamperedPair, AAD_PAIR_REQUEST);
  }, "변조된 페어링 봉투 거부");

  await expectThrows(function () {
    return E2E.openPair(pairKey, "AAAA", AAD_PAIR_REQUEST);
  }, "너무 짧은 페어링 봉투 거부");

  // --- 2d. 카운터 지속성(새로고침/탭 간 단조 증가) --------------------------
  E2E.configure({
    deviceId: vectors.device_id,
    deviceToken: "DEVICE-TOKEN",
    appPublicB64: vectors.app.public
  });
  E2E.clearCounter();
  await E2E.encodeRequest({ method: "GET", path: "/api/one" });
  await E2E.encodeRequest({ method: "GET", path: "/api/two" });
  E2E.configure({
    deviceId: vectors.device_id,
    deviceToken: "DEVICE-TOKEN",
    appPublicB64: vectors.app.public
  });
  var afterReload = await E2E.encodeRequest({ method: "GET", path: "/api/three" });
  var reloadCounter = (await parseRequestEnvelope(sessionKeyBytes, afterReload)).counter;
  ok(reloadCounter === 2, "새로고침 후에도 카운터가 이어진다(0으로 리셋되지 않음)");

  E2E.reset();
  E2E.configure({
    deviceId: vectors.device_id,
    deviceToken: "DEVICE-TOKEN",
    appPublicB64: vectors.app.public
  });
  var afterReset = await E2E.encodeRequest({ method: "GET", path: "/api/four" });
  ok(
    (await parseRequestEnvelope(sessionKeyBytes, afterReset)).counter === 3,
    "reset() 은 저장된 카운터를 유지한다"
  );

  E2E.clearCounter();
  var afterClear = await E2E.encodeRequest({ method: "GET", path: "/api/five" });
  ok(
    (await parseRequestEnvelope(sessionKeyBytes, afterClear)).counter === 0,
    "clearCounter() 후 0부터 다시 시작(재페어링용)"
  );

  var explicit = await E2E.encodeRequest({ method: "GET", path: "/api/six" });
  E2E.configure({
    deviceId: vectors.device_id,
    deviceToken: "DEVICE-TOKEN",
    appPublicB64: vectors.app.public,
    requestCounter: 100
  });
  var fromExplicit = await E2E.encodeRequest({ method: "GET", path: "/api/seven" });
  ok(
    (await parseRequestEnvelope(sessionKeyBytes, fromExplicit)).counter === 100,
    "requestCounter 옵션으로 시작값 지정"
  );
  ok(explicit !== null, "명시 카운터 이전 요청도 정상 발급");

  return passed;
}

main().then(
  function (count) {
    console.log("E2E 벡터 테스트 통과: " + count + "개");
  },
  function (error) {
    console.error("E2E 벡터 테스트 실패: " + (error && error.message ? error.message : error));
    process.exitCode = 1;
  }
);
