// In-frame crop runtime: `ns.capture(rect)` captures the picked page-coordinate
// rect with @zumer/snapdom and returns a JPEG data URL within the parent's caps
// (design §2.9). The snapdom call pattern follows huggingface/chat-ui
// artifactCapture.ts (Apache-2.0). `ns.snapdom` is the reference runtimeSources
// captured when the snapdom IIFE was evaluated.

var CAPTURE_TIMEOUT_MS = 5000;
var MAX_LONG_EDGE = 1600;
var MAX_DATA_BYTES = 2 * 1024 * 1024;
var JPEG_QUALITIES = [0.85, 0.7, 0.5];
var OVERLAY_HOST_ID = "__omni-annotate-host";
var SHIELD_ID = "__omni-annotate-shield";

function timeoutAfter(promise, ms) {
  return new Promise(function (resolve, reject) {
    var timer = setTimeout(function () {
      reject(new Error("capture timed out"));
    }, ms);
    promise.then(
      function (value) {
        clearTimeout(timer);
        resolve(value);
      },
      function (error) {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

// Overlapping captures nest: only the outermost hides the chrome and only the
// outermost restore puts the original visibility back.
var chromeHideDepth = 0;
var restoreChrome = null;

/** Hide our chrome for the capture; the returned callback joins the depth. */
function hideOwnChrome() {
  if (chromeHideDepth === 0) {
    var hidden = [];
    var ids = [OVERLAY_HOST_ID, SHIELD_ID];
    for (var i = 0; i < ids.length; i++) {
      var node = document.getElementById(ids[i]);
      if (!node) continue;
      hidden.push([node, node.style.visibility]);
      node.style.visibility = "hidden";
    }
    restoreChrome = function () {
      for (var j = 0; j < hidden.length; j++) {
        hidden[j][0].style.visibility = hidden[j][1];
      }
    };
  }
  chromeHideDepth++;
  return function () {
    if (chromeHideDepth === 0) return;
    chromeHideDepth--;
    if (chromeHideDepth === 0) {
      restoreChrome();
      restoreChrome = null;
    }
  };
}

/** The picked rect in page coordinates, clipped to the document's scroll box. */
function clipRect(rect) {
  var doc = document.documentElement;
  var body = document.body;
  var docW = Math.max(doc ? doc.scrollWidth : 0, body ? body.scrollWidth : 0);
  var docH = Math.max(doc ? doc.scrollHeight : 0, body ? body.scrollHeight : 0);
  var x = Math.max(0, rect.x);
  var y = Math.max(0, rect.y);
  var right = rect.x + rect.w;
  var bottom = rect.y + rect.h;
  if (docW > 0) right = Math.min(right, docW);
  if (docH > 0) bottom = Math.min(bottom, docH);
  return {
    x: x,
    y: y,
    width: Math.max(1, right - x),
    height: Math.max(1, bottom - y),
  };
}

/** Body background for the JPEG fill; explicit white when the body is clear. */
function pageBackground() {
  var color = "";
  try {
    if (document.body) color = getComputedStyle(document.body).backgroundColor || "";
  } catch {
    color = "";
  }
  if (!color || color === "transparent" || color === "rgba(0, 0, 0, 0)") return "#ffffff";
  return color;
}

/** Shrink so the long edge fits MAX_LONG_EDGE; the source canvas otherwise. */
function fitCanvas(source) {
  var longEdge = Math.max(source.width, source.height);
  if (longEdge <= MAX_LONG_EDGE) return source;
  var scale = MAX_LONG_EDGE / longEdge;
  var target = document.createElement("canvas");
  target.width = Math.max(1, Math.round(source.width * scale));
  target.height = Math.max(1, Math.round(source.height * scale));
  var context = target.getContext("2d");
  if (!context) return source;
  context.drawImage(source, 0, 0, target.width, target.height);
  return target;
}

/** JPEG data URL at the first quality whose decoded size fits the cap. */
function encodeJpeg(canvas) {
  var dataUrl;
  for (var i = 0; i < JPEG_QUALITIES.length; i++) {
    dataUrl = canvas.toDataURL("image/jpeg", JPEG_QUALITIES[i]);
    var base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
    if (Math.floor((base64.length * 3) / 4) <= MAX_DATA_BYTES) return dataUrl;
  }
  return null;
}

async function captureRegion(rect) {
  var root = document.body || document.documentElement;
  var canvas = await ns.snapdom.toCanvas(root, {
    clip: clipRect(rect),
    dpr: 1,
    embedFonts: true,
    fast: true,
    backgroundColor: pageBackground(),
    placeholders: true,
    exclude: "#" + OVERLAY_HOST_ID,
  });
  var fitted = fitCanvas(canvas);
  var dataUrl = encodeJpeg(fitted);
  if (!dataUrl) return null;
  return { dataUrl: dataUrl, width: fitted.width, height: fitted.height };
}

ns.capture = async function (rect) {
  var restore = hideOwnChrome();
  try {
    if (!ns.snapdom || typeof ns.snapdom.toCanvas !== "function") return null;
    return await timeoutAfter(captureRegion(rect), CAPTURE_TIMEOUT_MS);
  } catch (error) {
    var reason = error && error.message ? error.message : error;
    console.warn("[omni-annotate] screenshot capture failed:", reason);
    return null;
  } finally {
    restore();
  }
};
