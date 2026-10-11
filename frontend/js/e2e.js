/* LAFhvst 종단간 암호화(E2E v2) — 폰(PWA) 측 WebCrypto 구현.
   계약: relay/protocol.md §9, 파이썬 참조: core/remote_crypto.py.
   브라우저와 Node 양쪽에서 로드 가능하며 외부 의존성이 없다. */
(function (root) {
  "use strict";

  var STORE_PRIVATE_JWK = "lafE2EPrivateJwk";
  var STORE_PUBLIC_B64 = "lafE2EPublicB64";

  var MAGIC = 0x02;
  var NONCE_BYTES = 12;
  var TAG_BYTES = 16;
  var KEY_BITS = 256;
  var MAX_BODY_BYTES = 6 * 1024 * 1024;

  var PAIR_INFO = "lafhvst-pair-v2";
  var SESSION_SALT = "lafhvst-e2e-v1";
  var SESSION_INFO_PREFIX = "device:";
  var AAD_PAIR_REQUEST = "lafhvst-pair-v2|req|";
  var AAD_PAIR_RESPONSE = "lafhvst-pair-v2|res|";
  var AAD_SESSION_REQUEST = "lafhvst-e2e-v1|req|";
  var AAD_SESSION_RESPONSE = "lafhvst-e2e-v1|res|";
  var DIRECTION_REQUEST = 0;
  var DIRECTION_RESPONSE = 1;
  var E2E_PATH = "/api/e2e";

  var textEncoder = typeof TextEncoder !== "undefined" ? new TextEncoder() : null;
  var textDecoder = typeof TextDecoder !== "undefined" ? new TextDecoder() : null;

  var session = {
    deviceId: 0,
    deviceToken: "",
    appPublicB64: "",
    requestCounter: 0,
    responseCounter: -1,
    sessionKey: null
  };

  var identityCache = { publicB64: "", privateJwk: null, privateKey: null };

  // --- 기본 유틸 -----------------------------------------------------------
  function cryptoObject() {
    return root.crypto && root.crypto.subtle ? root.crypto : null;
  }

  function supported() {
    var object = cryptoObject();
    if (!object) {
      return false;
    }
    return (
      typeof object.subtle.importKey === "function" &&
      typeof object.subtle.deriveBits === "function" &&
      typeof object.subtle.encrypt === "function" &&
      typeof object.getRandomValues === "function"
    );
  }

  function ensureCrypto() {
    if (!supported()) {
      return Promise.reject(new Error("이 브라우저에서 WebCrypto(ECDH)를 사용할 수 없습니다"));
    }
    return Promise.resolve();
  }

  function utf8Encode(text) {
    return textEncoder.encode(String(text));
  }

  function utf8Decode(bytes) {
    return textDecoder.decode(bytes);
  }

  function jsonToBytes(value) {
    return utf8Encode(JSON.stringify(value));
  }

  function bytesToJson(bytes) {
    return JSON.parse(utf8Decode(bytes));
  }

  function b64urlEncode(bytes) {
    var binary = "";
    for (var i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return root.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function b64urlDecode(text) {
    var value = String(text === null || text === undefined ? "" : text).replace(/-/g, "+").replace(/_/g, "/");
    var remainder = value.length % 4;
    if (remainder === 2) {
      value += "==";
    } else if (remainder === 3) {
      value += "=";
    }
    var binary = root.atob(value);
    var out = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) {
      out[i] = binary.charCodeAt(i);
    }
    return out;
  }

  function toBytes(value) {
    if (value === null || value === undefined) {
      return new Uint8Array(0);
    }
    if (value instanceof Uint8Array) {
      return value;
    }
    if (typeof ArrayBuffer !== "undefined" && value instanceof ArrayBuffer) {
      return new Uint8Array(value);
    }
    if (ArrayBuffer.isView(value)) {
      return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    }
    throw new Error("바이트열이 아닙니다");
  }

  function concatBytes(left, right) {
    var out = new Uint8Array(left.length + right.length);
    out.set(left, 0);
    out.set(right, left.length);
    return out;
  }

  function randomBytes(length) {
    var out = new Uint8Array(length);
    cryptoObject().getRandomValues(out);
    return out;
  }

  function storageGet(key) {
    try {
      return root.localStorage ? root.localStorage.getItem(key) : null;
    } catch (error) {
      return null;
    }
  }

  function storageSet(key, value) {
    try {
      if (root.localStorage) {
        root.localStorage.setItem(key, value);
      }
    } catch (error) {
      /* 저장 실패(사생활 보호 모드 등)는 무시한다 */
    }
  }

  function normalizeHeaders(headers) {
    var out = {};
    if (!headers) {
      return out;
    }
    Object.keys(headers).forEach(function (key) {
      if (!key) {
        return;
      }
      var value = headers[key];
      out[String(key)] = String(value === null || value === undefined ? "" : value);
    });
    return out;
  }

  function hasHeader(headers, name) {
    var lower = name.toLowerCase();
    return Object.keys(headers).some(function (key) {
      return key.toLowerCase() === lower;
    });
  }

  // --- nonce / aad ---------------------------------------------------------
  function nonceFor(counter, direction) {
    var nonce = new Uint8Array(NONCE_BYTES);
    new DataView(nonce.buffer).setBigUint64(0, BigInt(counter));
    nonce[8] = direction & 0xff;
    return nonce;
  }

  function readCounter(nonce) {
    return Number(new DataView(nonce.buffer, nonce.byteOffset, 8).getBigUint64(0));
  }

  function aadFor(direction, deviceId) {
    var prefix = direction === DIRECTION_REQUEST ? AAD_SESSION_REQUEST : AAD_SESSION_RESPONSE;
    return utf8Encode(prefix + String(deviceId));
  }

  // --- 키 ------------------------------------------------------------------
  function importPrivateJwk(jwk) {
    var clean = {};
    Object.keys(jwk || {}).forEach(function (key) {
      clean[key] = jwk[key];
    });
    // 저장된 key_ops/ext 는 import 시 usages 와 충돌할 수 있어 정리한다.
    delete clean.key_ops;
    clean.ext = true;
    return cryptoObject().subtle.importKey(
      "jwk",
      clean,
      { name: "ECDH", namedCurve: "P-256" },
      true,
      ["deriveBits"]
    );
  }

  function importPublicKey(publicB64) {
    var der = b64urlDecode(publicB64);
    if (!der.length) {
      return Promise.reject(new Error("앱 공개키가 없습니다"));
    }
    return cryptoObject().subtle.importKey(
      "spki",
      der,
      { name: "ECDH", namedCurve: "P-256" },
      true,
      []
    );
  }

  function deriveShared(privateKey, peerPublicB64) {
    return importPublicKey(peerPublicB64).then(function (peerKey) {
      return cryptoObject().subtle.deriveBits(
        { name: "ECDH", public: peerKey },
        privateKey,
        KEY_BITS
      );
    });
  }

  function deriveAesKey(privateKey, peerPublicB64, saltBytes, infoText) {
    var subtle = cryptoObject().subtle;
    return deriveShared(privateKey, peerPublicB64)
      .then(function (shared) {
        return subtle.importKey("raw", new Uint8Array(shared), "HKDF", false, ["deriveKey"]);
      })
      .then(function (hkdfKey) {
        return subtle.deriveKey(
          { name: "HKDF", hash: "SHA-256", salt: saltBytes, info: utf8Encode(infoText) },
          hkdfKey,
          { name: "AES-GCM", length: KEY_BITS },
          false,
          ["encrypt", "decrypt"]
        );
      });
  }

  function readStoredIdentity() {
    var jwkText = storageGet(STORE_PRIVATE_JWK);
    var publicB64 = storageGet(STORE_PUBLIC_B64) || "";
    if (!jwkText || !publicB64) {
      return null;
    }
    try {
      return { privateJwk: JSON.parse(jwkText), publicB64: publicB64 };
    } catch (error) {
      return null;
    }
  }

  function snapshotIdentity() {
    return { publicB64: identityCache.publicB64, privateJwk: identityCache.privateJwk };
  }

  function createIdentity() {
    var subtle = cryptoObject().subtle;
    return subtle
      .generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"])
      .then(function (pair) {
        return Promise.all([
          subtle.exportKey("jwk", pair.privateKey),
          subtle.exportKey("spki", pair.publicKey)
        ]).then(function (exported) {
          var jwk = exported[0];
          var publicB64 = b64urlEncode(new Uint8Array(exported[1]));
          storageSet(STORE_PRIVATE_JWK, JSON.stringify(jwk));
          storageSet(STORE_PUBLIC_B64, publicB64);
          identityCache.privateKey = pair.privateKey;
          identityCache.privateJwk = jwk;
          identityCache.publicB64 = publicB64;
          return snapshotIdentity();
        });
      });
  }

  function ensureIdentity() {
    if (identityCache.privateKey) {
      return Promise.resolve(snapshotIdentity());
    }
    return ensureCrypto().then(function () {
      var stored =
        identityCache.privateJwk && identityCache.publicB64
          ? { privateJwk: identityCache.privateJwk, publicB64: identityCache.publicB64 }
          : readStoredIdentity();
      if (!stored) {
        return createIdentity();
      }
      return importPrivateJwk(stored.privateJwk).then(
        function (privateKey) {
          identityCache.privateKey = privateKey;
          identityCache.privateJwk = stored.privateJwk;
          identityCache.publicB64 = stored.publicB64;
          return snapshotIdentity();
        },
        function () {
          return createIdentity();
        }
      );
    });
  }

  function identity() {
    if (!identityCache.privateJwk || !identityCache.publicB64) {
      var stored = readStoredIdentity();
      if (!stored) {
        return null;
      }
      identityCache.privateJwk = stored.privateJwk;
      identityCache.publicB64 = stored.publicB64;
    }
    return snapshotIdentity();
  }

  // --- 세션 상태 -----------------------------------------------------------
  function configure(options) {
    options = options || {};
    session.deviceId = Number(options.deviceId) || 0;
    session.deviceToken =
      options.deviceToken === null || options.deviceToken === undefined ? "" : String(options.deviceToken);
    session.appPublicB64 =
      options.appPublicB64 === null || options.appPublicB64 === undefined ? "" : String(options.appPublicB64);
    session.requestCounter = nextCounterStart(session.deviceId, options.requestCounter);
    session.responseCounter = -1;
    session.sessionKey = null;
  }

  function counterStorageKey(deviceId) {
    return "lafE2ECounter:" + String(deviceId || 0);
  }

  function nextCounterStart(deviceId, explicit) {
    // 카운터는 기기별로 단조 증가해야 한다(앱이 재전송을 거부하므로 새로고침 후에도 이어져야 함).
    if (explicit !== null && explicit !== undefined && explicit !== "") {
      var value = Number(explicit);
      if (isFinite(value) && value >= 0) {
        return Math.floor(value);
      }
    }
    var stored = Number(storageGet(counterStorageKey(deviceId)));
    return isFinite(stored) && stored > 0 ? Math.floor(stored) : 0;
  }

  function clearCounter() {
    storageSet(counterStorageKey(session.deviceId), "");
    session.requestCounter = 0;
  }

  function configured() {
    return session.deviceId > 0 && !!session.appPublicB64 && !!session.deviceToken;
  }

  function reset() {
    // 세션 상태만 비운다(저장된 카운터는 유지 — 새로고침 후 이어서 사용).
    session.deviceId = 0;
    session.deviceToken = "";
    session.appPublicB64 = "";
    session.responseCounter = -1;
    session.sessionKey = null;
  }

  function assertConfigured() {
    if (!configured()) {
      throw new Error("E2E 세션이 설정되지 않았습니다(configure 필요)");
    }
  }

  function sessionKeyFor(deviceId) {
    if (session.sessionKey) {
      return Promise.resolve(session.sessionKey);
    }
    return ensureIdentity()
      .then(function () {
        return deriveAesKey(
          identityCache.privateKey,
          session.appPublicB64,
          utf8Encode(SESSION_SALT),
          SESSION_INFO_PREFIX + String(deviceId)
        );
      })
      .then(function (key) {
        session.sessionKey = key;
        return key;
      });
  }

  // --- 페어링 --------------------------------------------------------------
  function pairKeyFor(appPublicB64, pairToken) {
    var token = pairToken === null || pairToken === undefined ? "" : String(pairToken);
    return ensureCrypto()
      .then(function () {
        return ensureIdentity();
      })
      .then(function () {
        return deriveAesKey(
          identityCache.privateKey,
          String(appPublicB64 || ""),
          utf8Encode(token),
          PAIR_INFO
        );
      });
  }

  function sealPair(key, obj, aadText) {
    var nonce = randomBytes(NONCE_BYTES);
    return ensureCrypto()
      .then(function () {
        return cryptoObject().subtle.encrypt(
          { name: "AES-GCM", iv: nonce, additionalData: utf8Encode(aadText) },
          key,
          jsonToBytes(obj)
        );
      })
      .then(function (ciphertext) {
        return b64urlEncode(concatBytes(nonce, new Uint8Array(ciphertext)));
      });
  }

  function openPair(key, encB64, aadText) {
    return ensureCrypto().then(function () {
      var raw = b64urlDecode(encB64);
      if (raw.length <= NONCE_BYTES) {
        throw new Error("페어링 봉투가 너무 짧습니다");
      }
      return cryptoObject().subtle
        .decrypt(
          {
            name: "AES-GCM",
            iv: raw.subarray(0, NONCE_BYTES),
            additionalData: utf8Encode(aadText)
          },
          key,
          raw.subarray(NONCE_BYTES)
        )
        .then(function (plaintext) {
          return bytesToJson(new Uint8Array(plaintext));
        });
    });
  }

  // --- 세션 봉투 -----------------------------------------------------------
  function encodeRequest(obj) {
    obj = obj || {};
    return Promise.resolve().then(function () {
      assertConfigured();
      var body = toBytes(obj.body);
      if (body.length > MAX_BODY_BYTES) {
        throw new Error("요청 본문이 너무 큽니다");
      }
      var deviceId = session.deviceId;
      var counter = session.requestCounter;
      session.requestCounter = counter + 1;
      storageSet(counterStorageKey(deviceId), session.requestCounter);
      var nonce = nonceFor(counter, DIRECTION_REQUEST);
      var plaintext = jsonToBytes({
        method: String(obj.method || "GET"),
        path: String(obj.path || "/"),
        query: String(obj.query || ""),
        headers: normalizeHeaders(obj.headers),
        body_b64: b64urlEncode(body)
      });
      return sessionKeyFor(deviceId)
        .then(function (key) {
          return cryptoObject().subtle.encrypt(
            { name: "AES-GCM", iv: nonce, additionalData: aadFor(DIRECTION_REQUEST, deviceId) },
            key,
            plaintext
          );
        })
        .then(function (ciphertext) {
          var envelope = new Uint8Array(1 + 4 + NONCE_BYTES + ciphertext.byteLength);
          envelope[0] = MAGIC;
          new DataView(envelope.buffer).setUint32(1, deviceId);
          envelope.set(nonce, 5);
          envelope.set(new Uint8Array(ciphertext), 5 + NONCE_BYTES);
          return envelope;
        });
    });
  }

  function decodeResponse(bytes) {
    return Promise.resolve().then(function () {
      assertConfigured();
      var raw = toBytes(bytes);
      if (raw.length < 1 + NONCE_BYTES + TAG_BYTES) {
        throw new Error("응답 봉투가 너무 짧습니다");
      }
      if (raw[0] !== MAGIC) {
        throw new Error("알 수 없는 봉투 형식입니다");
      }
      var nonce = raw.subarray(1, 1 + NONCE_BYTES);
      if (nonce[8] !== DIRECTION_RESPONSE) {
        throw new Error("방향이 잘못된 봉투입니다");
      }
      var counter = readCounter(nonce);
      if (counter <= session.responseCounter) {
        throw new Error("응답 카운터가 재사용되었습니다");
      }
      var deviceId = session.deviceId;
      return sessionKeyFor(deviceId)
        .then(function (key) {
          return cryptoObject().subtle.decrypt(
            { name: "AES-GCM", iv: nonce, additionalData: aadFor(DIRECTION_RESPONSE, deviceId) },
            key,
            raw.subarray(1 + NONCE_BYTES)
          );
        })
        .then(function (plaintext) {
          var data = bytesToJson(new Uint8Array(plaintext));
          session.responseCounter = counter;
          return {
            counter: counter,
            status: Number(data.status || 0),
            headers: normalizeHeaders(data.headers),
            body: b64urlDecode(data.body_b64 || "")
          };
        });
    });
  }

  function request(options) {
    options = options || {};
    return Promise.resolve()
      .then(function () {
        assertConfigured();
        var headers = normalizeHeaders(options.headers);
        if (!hasHeader(headers, "authorization")) {
          headers.authorization = "Bearer " + session.deviceToken;
        }
        return encodeRequest({
          method: options.method,
          path: options.path,
          query: options.query,
          headers: headers,
          body: options.body
        });
      })
      .then(function (envelope) {
        var fetchFn = root.fetch || (typeof fetch !== "undefined" ? fetch : null);
        if (!fetchFn) {
          throw new Error("fetch 를 사용할 수 없습니다");
        }
        return fetchFn(E2E_PATH, {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream" },
          body: envelope
        });
      })
      .then(function (response) {
        if (!response || response.status !== 200) {
          throw new Error("E2E 요청이 실패했습니다 (HTTP " + (response ? response.status : "?") + ")");
        }
        return response.arrayBuffer().then(function (buffer) {
          return decodeResponse(new Uint8Array(buffer)).then(function (out) {
            return { status: out.status, headers: out.headers, body: out.body };
          });
        });
      });
  }

  root.E2E = {
    supported: supported,
    ensureIdentity: ensureIdentity,
    identity: identity,
    configure: configure,
    configured: configured,
    pairKeyFor: pairKeyFor,
    sealPair: sealPair,
    openPair: openPair,
    encodeRequest: encodeRequest,
    decodeResponse: decodeResponse,
    request: request,
    reset: reset,
    clearCounter: clearCounter
  };
})(typeof window !== "undefined" ? window : globalThis);
