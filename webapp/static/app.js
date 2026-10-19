'use strict';

/* 本地雷达 · Web 前端（单文件，无构建、无外部依赖）
 *
 * 为什么这么"素"：服务端下发的 CSP 是 `default-src 'self'`，
 * 任何 CDN、内联脚本、内联 style 属性都会被浏览器直接拦掉，
 * 所以字体只用系统栈，所有样式走 app.css，所有逻辑走本文件。
 * 动态几何只通过 CSSOM（el.style.x / setProperty）写，不碰 style 属性与 eval。
 *
 * 分工（见 docs/WEB.md "关键取舍"）：用户可见文案一律取 payload 的 display 子对象，
 * JS 只用原始量算几何（投影、插值、航向）。在 JS 里重算单位与格式，
 * 迟早会和服务端的 Flight.display_* 漂移成两套文案。
 *
 * 投影必须与 app/geo.py 的 Projector 完全一致（方位等距投影：先算大圆距离与初始方位，
 * 再按 px_per_km 落点），否则底图位图与目标图标会错开——这不是调参问题，是同一套公式。
 */

// ====================== 常量 ======================

var CSRF_HEADERS = { 'X-Requested-With': 'local-radar' };

/* 桌面版的 16 档量程，索引即档位；服务端只收浮点 km，档位换算留在客户端。 */
var RANGE_STOPS = [2, 5, 10, 15, 20, 25, 30, 50, 75, 100, 150, 200, 250, 300, 400, 460];

/* 全部定位手段都失败时的兜底中心，取 docs/WEB.md 示例里的坐标。 */
var FALLBACK_CENTER = { lat: 22.30314, lon: 114.178832 };

/* 与 app/geo.py 同名常量同值：改了这里底图就会和目标错位。 */
var EARTH_RADIUS_KM = 6371.0088;
var RING_STEP_CANDIDATES = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000];
var MIN_RINGS = 4;
var MAX_RINGS = 6;

var SWEEP_PERIOD_MS = 6000;
var SWEEP_TAIL_STEPS = 26;
/* 命中半径按 CSS 像素算：22px 在触屏上点不中，44px 才等于指尖宽度。 */
var TAP_RADIUS_PX = 44;
var TAP_SLOP_PX = 12;

var BASEMAP_SIZE = 768;
/* 底图渲染模块可能还没就绪（503），失败后压一段时间再试，不要每次重绘都打服务端。 */
var BASEMAP_RETRY_MS = 60000;

var LIST_ROWS = 10;

/* 画布内文字只用等宽字体：秒针与刻度数字跳动时字宽不变，画面才不抖。 */
var MONO_FONT = 'ui-monospace, "Cascadia Mono", Consolas, "SFMono-Regular", monospace';

/* 服务端只回数据源标识符（app/data/sources.py 的 available_sources），
 * 没有对应的 display 文案，短名映射只能落在客户端。 */
var SOURCE_LABELS = {
  adsbfi: 'adsb.fi',
  adsblol: 'adsb.lol',
  opensky: 'OpenSky',
  fr24: 'FR24'
};

/* 底图图层 → 配置键。layers 白名单见 webapp/settings.py:BASEMAP_LAYERS。 */
var LAYER_KEYS = [
  ['terrain', 'show_terrain'],
  ['roads', 'show_roads'],
  ['clouds', 'show_clouds'],
  ['places', 'show_places'],
  ['airports', 'show_airports']
];

var TOGGLE_LABELS = {
  show_ground: '显示地面目标',
  show_terrain: '地形着色',
  show_roads: '路网',
  show_clouds: '云图',
  show_places: '地名',
  show_airports: '机场',
  show_weather: '天气'
};

/* 数值型配置的可调区间。服务端 /api/state 只回当前值、不回区间，
 * 这里必须与 app/config.py 的 NUMERIC_LIMITS 保持一致（越界值会被服务端夹紧，
 * 但界面还是不该允许拖到非法值上）。 */
var NUMERIC_RANGES = {
  refresh_sec: { min: 3, max: 300, step: 1, unit: '秒' },
  max_flights: { min: 5, max: 200, step: 5, unit: '架' },
  stale_limit_sec: { min: 10, max: 600, step: 10, unit: '秒' }
};

// ====================== 小工具 ======================

function $(id) {
  return document.getElementById(id);
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function toRad(deg) {
  return (deg * Math.PI) / 180;
}

function toDeg(rad) {
  return (rad * 180) / Math.PI;
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

/* 经度插值必须绕短边：跨 180° 时朴素线性插值会让目标绕地球飞一圈。 */
function lerpLon(a, b, t) {
  var delta = ((b - a + 540) % 360) - 180;
  var value = a + delta * t;
  return ((value + 540) % 360) - 180;
}

function haversineKm(lat1, lon1, lat2, lon2) {
  var phi1 = toRad(lat1);
  var phi2 = toRad(lat2);
  var dphi = phi2 - phi1;
  var dlam = toRad(lon2 - lon1);
  var h = Math.sin(dphi * 0.5) * Math.sin(dphi * 0.5) +
    Math.cos(phi1) * Math.cos(phi2) * Math.sin(dlam * 0.5) * Math.sin(dlam * 0.5);
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(h)));
}

function initialBearing(lat1, lon1, lat2, lon2) {
  var phi1 = toRad(lat1);
  var phi2 = toRad(lat2);
  var dlam = toRad(lon2 - lon1);
  var y = Math.sin(dlam) * Math.cos(phi2);
  var x = Math.cos(phi1) * Math.sin(phi2) -
    Math.sin(phi1) * Math.cos(phi2) * Math.cos(dlam);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}

function ringStepKm(radiusKm) {
  for (var i = 0; i < RING_STEP_CANDIDATES.length; i += 1) {
    var step = RING_STEP_CANDIDATES[i];
    if (radiusKm / step >= MIN_RINGS && radiusKm / step <= MAX_RINGS) {
      return step;
    }
  }
  var best = RING_STEP_CANDIDATES[0];
  for (var j = 1; j < RING_STEP_CANDIDATES.length; j += 1) {
    if (Math.abs(RING_STEP_CANDIDATES[j] / radiusKm - 0.2) <
        Math.abs(best / radiusKm - 0.2)) {
      best = RING_STEP_CANDIDATES[j];
    }
  }
  return best;
}

function ringValuesKm(radiusKm) {
  var step = ringStepKm(radiusKm);
  var values = [];
  var value = step;
  while (value < radiusKm - step * 0.35) {
    values.push(value);
    value += step;
  }
  return values;
}

function hexToRgb(hex) {
  var text = String(hex || '').replace('#', '');
  if (text.length !== 6) {
    return { r: 63, g: 224, b: 138 };
  }
  return {
    r: parseInt(text.slice(0, 2), 16),
    g: parseInt(text.slice(2, 4), 16),
    b: parseInt(text.slice(4, 6), 16)
  };
}

function rgba(hex, alpha) {
  var rgb = hexToRgb(hex);
  return 'rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',' + alpha + ')';
}

function nearestRangeIndex(radiusKm) {
  var best = 0;
  var bestDelta = Infinity;
  for (var i = 0; i < RANGE_STOPS.length; i += 1) {
    var delta = Math.abs(RANGE_STOPS[i] - radiusKm);
    if (delta < bestDelta) {
      bestDelta = delta;
      best = i;
    }
  }
  return best;
}

function sourceLabel(id) {
  if (!id) {
    return '未知源';
  }
  return SOURCE_LABELS[id] || id;
}

function formatClock(isoText) {
  if (!isoText) {
    return '—';
  }
  var date = new Date(isoText);
  if (isNaN(date.getTime())) {
    return '—';
  }
  var pad = function (value) {
    return value < 10 ? '0' + value : String(value);
  };
  return pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds());
}

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    /* 一律 textContent：payload 里的字符串（航班号、航司名）绝不进 innerHTML。 */
    node.textContent = text;
  }
  return node;
}

function flightName(flight) {
  var display = flight.display || {};
  return display.flight_number || display.icao_callsign ||
    String(flight.icao24 || '').toUpperCase();
}

function isMobileLayout() {
  return window.matchMedia('(max-width: 859px)').matches;
}

// ====================== 状态 ======================

var state = {
  config: null,
  sources: [],
  cooling: {},
  center: null,
  locationKind: '',
  locationLabel: '定位中…',
  radiusIndex: 8,
  tracks: new Map(),
  frame: { flights: [], source: '', notice: '', stale: false, fetchedAt: null },
  selected: null,
  detail: null,
  detailLoading: false,
  localNotice: '',
  basemap: { key: '', image: null, failedAt: 0 },
  refreshMs: 10000,
  pollTimer: 0,
  pollInFlight: false,
  running: false,
  tab: 'list',
  view: { width: 0, height: 0, cx: 0, cy: 0, radiusPx: 0, dpr: 1, topStrip: 0 },
  sheetHeight: 0
};

var dom = {};
var settingsControls = {};
var basemapRequest = 0;

// ====================== 网络层 ======================

function ApiError(status, message) {
  this.name = 'ApiError';
  this.status = status;
  this.message = message || ('HTTP ' + status);
}

/**
 * 统一的请求入口。
 *
 * 两件事必须每处都做对，所以只留一个出口：Cookie 用 same-origin 带上（httpOnly，
 * 前端碰不到 token），写操作一律补 CSRF 头（跨站表单无法自定义请求头）。
 */
function api(path, options) {
  var init = options || {};
  var headers = {};
  var key = null;
  for (key in (init.headers || {})) {
    if (Object.prototype.hasOwnProperty.call(init.headers, key)) {
      headers[key] = init.headers[key];
    }
  }
  var method = (init.method || 'GET').toUpperCase();
  if (method !== 'GET' && method !== 'HEAD') {
    headers['X-Requested-With'] = CSRF_HEADERS['X-Requested-With'];
  }
  if (init.body !== undefined && init.body !== null) {
    headers['Content-Type'] = 'application/json';
  }
  var request = {
    method: method,
    credentials: 'same-origin',
    headers: headers
  };
  if (!init.noStore) {
    request.cache = 'no-store';
  }
  if (init.body !== undefined && init.body !== null) {
    request.body = init.body;
  }
  return fetch(path, request).then(function (response) {
    if (response.status === 401) {
      throw new ApiError(401, '请先登录');
    }
    return response;
  }, function () {
    throw new ApiError(0, '网络不可达');
  });
}

function readError(response) {
  return response.json().then(function (data) {
    var message = data && (data.error || data.message);
    throw new ApiError(response.status, message || ('HTTP ' + response.status));
  }, function () {
    throw new ApiError(response.status, 'HTTP ' + response.status);
  });
}

function apiJson(path, options) {
  return api(path, options).then(function (response) {
    if (!response.ok) {
      return readError(response);
    }
    return response.json();
  });
}

/** 任何一条 API 回 401 都意味着会话没了（过期/被撤），直接退回登录页。 */
function handleError(error) {
  if (error && error.status === 401) {
    showLogin(error.message || '请重新登录');
    return;
  }
  state.localNotice = (error && error.message) ? error.message : '请求失败';
  renderNotices();
}

// ====================== 登录 ======================

function showLogin(message) {
  state.running = false;
  if (state.pollTimer) {
    window.clearTimeout(state.pollTimer);
    state.pollTimer = 0;
  }
  dom.app.hidden = true;
  dom.login.hidden = false;
  dom.loginError.textContent = message || '';
  dom.password.value = '';
  state.detail = null;
  state.selected = null;
  if (window.matchMedia('(max-width: 859px)').matches) {
    document.documentElement.style.setProperty('--sheet-h', (window.innerHeight * 0.46) + 'px');
  }
}

function submitLogin(event) {
  event.preventDefault();
  var password = dom.password.value;
  if (!password) {
    dom.loginError.textContent = '请输入口令';
    return;
  }
  dom.loginError.textContent = '';
  dom.loginButton.disabled = true;
  /* 口令只在这一次请求里存在：不写 localStorage、不写 cookie、不进 URL。 */
  apiJson('/api/session', {
    method: 'POST',
    body: JSON.stringify({ password: password })
  }).then(function () {
    dom.password.value = '';
    dom.loginButton.disabled = false;
    return boot();
  }, function (error) {
    dom.loginButton.disabled = false;
    dom.password.value = '';
    if (error && error.status === 429) {
      dom.loginError.textContent = error.message || '尝试过多，请稍后再试';
    } else if (error && error.status === 401) {
      dom.loginError.textContent = error.message || '口令不正确';
    } else {
      dom.loginError.textContent = (error && error.message) || '登录失败';
    }
  });
}

function logout() {
  api('/api/session', { method: 'DELETE' }).then(function () {
    showLogin('');
  }, function () {
    showLogin('');
  });
}

// ====================== 引导 ======================

function applyConfig(config) {
  state.config = config;
  state.radiusIndex = nearestRangeIndex(config.radius_km);
  state.refreshMs = Math.max(3000, Number(config.refresh_sec) * 1000);
  document.documentElement.style.setProperty('--scan', config.scan_color);
  dom.rangeSlider.value = String(state.radiusIndex);
  renderRangeLabel();
}

function boot() {
  return apiJson('/api/state').then(function (data) {
    state.sources = data.sources || [];
    state.cooling = data.cooling || {};
    applyConfig(data.config);
    dom.app.hidden = false;
    dom.login.hidden = true;
    state.running = true;
    buildSettings(data);
    dom.listEmpty.hidden = false;
    dom.listEmpty.textContent = '加载中…';

    /* 定位可能等用户授权（最长 8 秒），先用兜底中心把界面画出来，
     * 定位一到就换过去——不能让雷达空着等授权。 */
    var fallback = manualFromState(data) || FALLBACK_CENTER;
    state.center = { lat: fallback.lat, lon: fallback.lon };
    state.locationKind = manualFromState(data) ? 'manual' : 'fallback';
    state.locationLabel = manualFromState(data) ? '配置里的手动坐标' : '默认位置';
    renderLocationBadge();
    resizeCanvas();
    startPolling();
    fetchBasemap();
    resolveLocation();

    return null;
  }, function (error) {
    if (error && error.status === 401) {
      showLogin('');
    } else {
      showLogin((error && error.message) || '无法获取服务状态');
    }
  });
}

// ====================== 定位 ======================

function manualFromState(data) {
  if (data && data.location && data.location.manual) {
    return { lat: data.location.manual[0], lon: data.location.manual[1] };
  }
  var config = data && data.config;
  if (config && config.location && typeof config.location.lat === 'number' &&
      typeof config.location.lon === 'number') {
    return { lat: config.location.lat, lon: config.location.lon };
  }
  return null;
}

function browserLocation() {
  return new Promise(function (resolve) {
    if (!navigator.geolocation) {
      resolve(null);
      return;
    }
    var settled = false;
    var finish = function (value) {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    /* 授权弹窗可能一直挂着不点：必须自己设超时，否则界面永远停在"定位中"。 */
    var timer = window.setTimeout(function () {
      finish(null);
    }, 8000);
    navigator.geolocation.getCurrentPosition(function (position) {
      window.clearTimeout(timer);
      finish({
        lat: position.coords.latitude,
        lon: position.coords.longitude,
        accuracy: position.coords.accuracy
      });
    }, function () {
      window.clearTimeout(timer);
      finish(null);
    }, { enableHighAccuracy: false, timeout: 7000, maximumAge: 300000 });
  });
}

/** 浏览器定位被拒/不可用时的兜底：服务端按 IP 粗定位。 */
function serverLocation() {
  return apiJson('/api/locate').then(function (data) {
    if (data && data.location && typeof data.location.lat === 'number') {
      return { lat: data.location.lat, lon: data.location.lon, source: data.location.source };
    }
    return null;
  }, function (error) {
    if (error && error.status === 401) {
      throw error;
    }
    return null;
  });
}

function resolveLocation() {
  state.locationLabel = '定位中…';
  renderLocationBadge();
  return browserLocation().then(function (browser) {
    if (browser) {
      return { point: browser, kind: 'browser', label: '浏览器定位' + accuracyText(browser.accuracy) };
    }
    return serverLocation().then(function (server) {
      if (server) {
        return { point: server, kind: 'server', label: '服务端 IP 定位' };
      }
      var manual = manualFromState({ config: state.config, location: null });
      if (manual) {
        return { point: manual, kind: 'manual', label: '配置里的手动坐标' };
      }
      return { point: FALLBACK_CENTER, kind: 'fallback', label: '默认位置' };
    });
  }).then(function (result) {
    setCenter(result);
    return result;
  }, function (error) {
    /* 这里的失败必须自己吞掉：它是个没人 await 的浮空 Promise，
     * 抛出去就会变成 unhandled rejection，定位失败还顺带把界面搞成没人管的状态。 */
    if (error && error.status === 401) {
      showLogin('登录已过期，请重新登录');
      return;
    }
    setCenter({ point: FALLBACK_CENTER, kind: 'fallback', label: '默认位置' });
  });
}

function accuracyText(accuracy) {
  if (typeof accuracy !== 'number' || !isFinite(accuracy)) {
    return '';
  }
  return '（±' + Math.round(accuracy) + ' m）';
}

function setCenter(result) {
  state.center = { lat: result.point.lat, lon: result.point.lon };
  state.locationKind = result.kind;
  state.locationLabel = result.label;
  renderLocationBadge();
  /* 换了圆心 = 换了数据集，插值轨道全部作废，否则旧目标会从旧位置飞过来。 */
  state.tracks.clear();
  state.basemap.key = '';
  state.basemap.failedAt = 0;
  fetchBasemap();
  pollFlights();
}

function renderLocationBadge() {
  dom.locationBadge.textContent = '定位：' + state.locationLabel;
  /* 落到兜底坐标时标黄：圆心很可能根本不是用户所在的地方，必须一眼看出来 */
  dom.locationBadge.classList.toggle('chip-fallback',
    state.locationKind === 'fallback');
  syncTopStrip();
}

// ====================== 轮询与插值 ======================

function startPolling() {
  if (state.pollTimer) {
    window.clearTimeout(state.pollTimer);
  }
  state.pollTimer = window.setTimeout(function () {
    pollFlights();
  }, state.refreshMs);
}

function currentRadiusKm() {
  return RANGE_STOPS[state.radiusIndex];
}

function queryFor(lat, lon, radiusKm) {
  return 'lat=' + lat.toFixed(6) + '&lon=' + lon.toFixed(6) +
    '&radius_km=' + radiusKm.toFixed(2);
}

function pollFlights() {
  if (!state.running || !state.center) {
    return Promise.resolve();
  }
  if (state.pollInFlight) {
    return Promise.resolve();
  }
  state.pollInFlight = true;
  var query = queryFor(state.center.lat, state.center.lon, currentRadiusKm());
  return apiJson('/api/flights?' + query).then(function (frame) {
    state.pollInFlight = false;
    state.localNotice = '';
    applyFrame(frame);
    startPolling();
    if (state.tab === 'detail' && state.selected) {
      loadDetail(state.selected, true);
    }
    return null;
  }, function (error) {
    state.pollInFlight = false;
    if (error && error.status === 401) {
      showLogin('登录已过期，请重新登录');
      return;
    }
    state.localNotice = (error && error.message) || '取数失败';
    renderNotices();
    startPolling();
  });
}

/**
 * 把一帧数据装进渲染状态，并为每个目标建一条插值轨道。
 *
 * 轨道起点取服务端的 prev_lat/prev_lon（上一轮上游位置），终点取本轮 lat/lon，
 * 时长取刷新间隔——这样目标是"滑"过去的。prev_lat 为空说明这是首次见到它，
 * 必须直接落点，否则它会从某个莫名其妙的地方飞进场。
 */
function applyFrame(frame) {
  var flights = frame.flights || [];
  var sorted = flights.slice().sort(function (a, b) {
    return (a.distance_km || 0) - (b.distance_km || 0);
  });
  var duration = state.refreshMs;
  var seen = new Set();
  var tracks = new Map();

  sorted.forEach(function (flight) {
    var icao = String(flight.icao24 || '').toLowerCase();
    if (!icao) {
      return;
    }
    seen.add(icao);
    var hasPrev = typeof flight.prev_lat === 'number' && typeof flight.prev_lon === 'number';
    tracks.set(icao, {
      flight: flight,
      fromLat: hasPrev ? flight.prev_lat : flight.lat,
      fromLon: hasPrev ? flight.prev_lon : flight.lon,
      toLat: flight.lat,
      toLon: flight.lon,
      durationMs: hasPrev ? duration : 0,
      startedAt: performance.now()
    });
  });

  state.tracks = tracks;
  state.frame = {
    flights: sorted,
    source: frame.source || '',
    notice: frame.notice || '',
    stale: !!frame.stale,
    fetchedAt: frame.fetched_at || null
  };
  state.cooling = frame.cooling || {};
  if (state.selected && !seen.has(state.selected)) {
    state.selected = null;
    renderList();
  }
  renderList();
  renderStatus();
  renderCooling();
}

/** 目标当前应画的位置：在轨道上按经过的时间插值。 */
function trackPosition(track, now) {
  if (!track.durationMs) {
    return { lat: track.toLat, lon: track.toLon };
  }
  var t = clamp((now - track.startedAt) / track.durationMs, 0, 1);
  /* 缓入缓出：等速插值在每轮起点会有肉眼可见的顿挫 */
  var eased = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
  return {
    lat: lerp(track.fromLat, track.toLat, eased),
    lon: lerpLon(track.fromLon, track.toLon, eased)
  };
}

function renderNotices() {
  var text = state.localNotice || state.frame.notice || '';
  dom.noticeBar.textContent = text;
  dom.noticeBar.hidden = !text;
  dom.staleBadge.hidden = !state.frame.stale;
  syncTopStrip();
}

/**
 * 顶部 HTML 提示条（定位来源 / 数据延迟 / 服务端 notice）压在画布上，
 * 画布里的状态行不能与它们叠字。把提示条的下边缘换算成画布内坐标存下来，
 * 状态行取"盘顶"与"提示条下沿"两者中的更低者。
 */
function syncTopStrip() {
  if (!dom.noticeBar) {
    return;
  }
  var canvasRect = dom.radar.getBoundingClientRect();
  if (!canvasRect.height) {
    state.view.topStrip = 0;
    return;
  }
  var bottom = dom.locationBadge.getBoundingClientRect().bottom - canvasRect.top;
  if (!dom.noticeBar.hidden) {
    bottom = Math.max(bottom, dom.noticeBar.getBoundingClientRect().bottom - canvasRect.top);
  }
  state.view.topStrip = Math.max(0, bottom) + 12;
}

function renderStatus() {
  /* 数据源 · 架数 · 量程 · 更新时间。放在 DOM 里由布局引擎避让，
   * 不再画进 canvas —— 画在盘内会和操作按钮、航班标签抢位置。 */
  if (dom.statusLine) {
    var frame = state.frame || {};
    var flights = frame.flights || [];
    var parts = [
      sourceLabel(frame.source || (state.config ? state.config.source : '')),
      flights.length + ' 架',
      currentRadiusKm() + ' km',
      formatClock(frame.fetchedAt)
    ];
    if (frame.stale) {
      parts.push('数据延迟');
    }
    dom.statusLine.textContent = parts.join(' · ');
  }
  renderNotices();
}

function renderRangeLabel() {
  dom.rangeLabel.textContent = currentRadiusKm() + ' km';
  dom.rangeIn.disabled = state.radiusIndex >= RANGE_STOPS.length - 1;
  dom.rangeOut.disabled = state.radiusIndex <= 0;
}

function renderCooling() {
  var parts = [];
  var cooling = state.cooling || {};
  Object.keys(cooling).forEach(function (id) {
    var seconds = Number(cooling[id]);
    if (seconds > 0) {
      parts.push(sourceLabel(id) + ' ' + Math.round(seconds) + ' 秒');
    }
  });
  dom.coolingNote.textContent = parts.length ? ('上游冷却：' + parts.join('、')) : '上游冷却：无';
}

// ====================== 底图 ======================

function basemapLayers() {
  if (!state.config) {
    return '';
  }
  return LAYER_KEYS.filter(function (pair) {
    return !!state.config[pair[1]];
  }).map(function (pair) {
    return pair[0];
  }).join(',');
}

function basemapKey() {
  if (!state.center) {
    return '';
  }
  return [
    state.center.lat.toFixed(4),
    state.center.lon.toFixed(4),
    currentRadiusKm(),
    basemapLayers()
  ].join('|');
}

/**
 * 底图位图读取。
 *
 * 走 data: URL 而不是 URL.createObjectURL：CSP 的 img-src 只放行 'self' 与 data:，
 * blob: 不在白名单里，用 createObjectURL 会被浏览器拦掉（而且报错很不直观）。
 */
function blobToImage(blob) {
  return new Promise(function (resolve, reject) {
    var reader = new FileReader();
    reader.onload = function () {
      var image = new Image();
      image.onload = function () {
        resolve(image);
      };
      image.onerror = function () {
        reject(new ApiError(0, '底图解码失败'));
      };
      image.src = String(reader.result);
    };
    reader.onerror = function () {
      reject(new ApiError(0, '底图读取失败'));
    };
    reader.readAsDataURL(blob);
  });
}

/**
 * 取一张方形底图 PNG 并铺到盘下。按 (位置, 半径, 图层) 缓存：
 * 这些不变时位图不变，重画只是重绘同一张图，不必再打服务端。
 *
 * 503 是**预期内**的（webapp/render.py 还没写完）：降级成"暗盘 + 量程环 + 目标"，
 * 不弹错、不阻塞轮询，只在设置页留一行说明，并压 60 秒再重试。
 */
function fetchBasemap() {
  if (!state.running || !state.center || !state.config) {
    return;
  }
  var key = basemapKey();
  if (key === state.basemap.key && state.basemap.image) {
    return;
  }
  if (key === state.basemap.key && !state.basemap.image &&
      state.basemap.failedAt > 0 &&
      performance.now() - state.basemap.failedAt < BASEMAP_RETRY_MS) {
    return;
  }

  state.basemap.key = key;
  basemapRequest += 1;
  var requestId = basemapRequest;
  var url = '/api/basemap?lat=' + state.center.lat.toFixed(6) +
    '&lon=' + state.center.lon.toFixed(6) +
    '&radius_km=' + currentRadiusKm().toFixed(2) +
    '&size=' + BASEMAP_SIZE +
    '&layers=' + encodeURIComponent(basemapLayers());

  api(url, { noStore: true }).then(function (response) {
    if (requestId !== basemapRequest) {
      return null;
    }
    if (!response.ok) {
      throw new ApiError(response.status, '底图不可用');
    }
    return response.blob();
  }).then(function (blob) {
    if (!blob) {
      return null;
    }
    return blobToImage(blob);
  }).then(function (image) {
    /* 视口/图层在下载期间已经变了：这张图对应的是旧视野，丢掉比画错好。 */
    if (!image || requestId !== basemapRequest || key !== basemapKey()) {
      return;
    }
    state.basemap.image = image;
    state.basemap.failedAt = 0;
    renderBasemapNote();
  }, function (error) {
    if (requestId !== basemapRequest) {
      /* 旧视野的失败不该污染新视野的状态（更不该清掉已经拿到的新图） */
      return;
    }
    if (error && error.status === 401) {
      showLogin('登录已过期，请重新登录');
      return;
    }
    state.basemap.image = null;
    state.basemap.failedAt = performance.now();
    renderBasemapNote();
  });
}

function renderBasemapNote() {
  if (!dom.basemapNote) {
    return;
  }
  if (state.basemap.image) {
    dom.basemapNote.textContent = '底图已加载';
  } else if (state.basemap.failedAt) {
    dom.basemapNote.textContent = '底图渲染模块暂不可用，当前只显示量程环与目标';
  } else {
    dom.basemapNote.textContent = '底图加载中…';
  }
}

// ====================== 画布几何 ======================

var TAU = Math.PI * 2;

function resizeCanvas() {
  var rect = dom.radar.getBoundingClientRect();
  var dpr = clamp(window.devicePixelRatio || 1, 1, 2);
  var width = Math.max(1, Math.round(rect.width));
  var height = Math.max(1, Math.round(rect.height));
  var pixelWidth = Math.round(width * dpr);
  var pixelHeight = Math.round(height * dpr);
  if (dom.radar.width !== pixelWidth || dom.radar.height !== pixelHeight) {
    dom.radar.width = pixelWidth;
    dom.radar.height = pixelHeight;
  }
  state.view.dpr = dpr;
  state.view.width = width;
  state.view.height = height;
  computeDish();
  syncTopStrip();
}

function computeDish() {
  var view = state.view;
  /* 窄屏的抽屉会盖住画布下半部：可视区要减掉抽屉高度，
   * 否则圆心会落在抽屉底下，盘的上半圈露在外面、下半圈看不见。 */
  var visibleHeight = view.height;
  if (isMobileLayout() && state.sheetHeight > 0) {
    visibleHeight = Math.max(180, view.height - state.sheetHeight);
  }
  view.cx = view.width / 2;
  view.cy = visibleHeight / 2;
  /* 46px 外圈：罗盘刻度最长 11px、方位字母在 +18px、状态行在 +36px，
   * 三者都要留在盘外。早先取 34px 时南向的 S 会被画布下边缘裁掉。 */
  view.radiusPx = Math.max(40, Math.min(view.width, visibleHeight) / 2 - 46);
}

/** 与 app/geo.py Projector.project 同一套公式：方位等距投影。 */
function projectPoint(lat, lon) {
  var view = state.view;
  var distance = haversineKm(state.center.lat, state.center.lon, lat, lon);
  var bearing = toRad(initialBearing(state.center.lat, state.center.lon, lat, lon));
  var scale = distance * (view.radiusPx / currentRadiusKm());
  return {
    x: view.cx + scale * Math.sin(bearing),
    y: view.cy - scale * Math.cos(bearing)
  };
}

function trackScreen(track, now) {
  var position = trackPosition(track, now);
  var point = projectPoint(position.lat, position.lon);
  return { x: point.x, y: point.y, lat: position.lat, lon: position.lon };
}

// ====================== 渲染 ======================

function scanColor() {
  return (state.config && state.config.scan_color) || '#3FE08A';
}

function draw(now) {
  var ctx = dom.context;
  var view = state.view;
  if (!view.width || !view.height) {
    return;
  }
  var scan = scanColor();

  ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
  ctx.fillStyle = '#0B0E0F';
  ctx.fillRect(0, 0, view.width, view.height);

  ctx.save();
  ctx.beginPath();
  ctx.arc(view.cx, view.cy, view.radiusPx, 0, TAU);
  ctx.clip();
  ctx.fillStyle = '#0D1512';
  ctx.fill();
  drawBasemapLayer(ctx);
  drawRings(ctx, scan);
  drawCrosshair(ctx, scan);
  drawSweep(ctx, scan, now);
  drawTargets(ctx, scan, now);
  ctx.restore();

  drawOuterRings(ctx, scan);
  drawCompass(ctx, scan);
  drawCentre(ctx, scan);
  drawStatusLine(ctx, scan);
}

function drawBasemapLayer(ctx) {
  var image = state.basemap.image;
  if (!image) {
    return;
  }
  var view = state.view;
  /* 服务端渲染的是"半径 = 半径像素"的方形图，所以铺满圆的外接正方形即可对上。 */
  ctx.save();
  ctx.globalAlpha = 0.92;
  ctx.drawImage(image,
    view.cx - view.radiusPx, view.cy - view.radiusPx,
    view.radiusPx * 2, view.radiusPx * 2);
  ctx.restore();
  /* 压暗一层：底图是彩色的，目标图标与绿色刻度必须还能一眼看出 */
  ctx.fillStyle = 'rgba(11, 14, 15, 0.45)';
  ctx.fillRect(view.cx - view.radiusPx, view.cy - view.radiusPx,
    view.radiusPx * 2, view.radiusPx * 2);
}

function haloText(ctx, text, x, y, color, font, align) {
  ctx.font = font;
  ctx.textAlign = align || 'center';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 3;
  ctx.strokeStyle = 'rgba(11, 14, 15, 0.85)';
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

function drawRings(ctx, scan) {
  var view = state.view;
  var radiusKm = currentRadiusKm();
  var font = '11px ' + MONO_FONT;
  ringValuesKm(radiusKm).forEach(function (value) {
    var px = (value / radiusKm) * view.radiusPx;
    ctx.beginPath();
    ctx.arc(view.cx, view.cy, px, 0, TAU);
    ctx.strokeStyle = rgba(scan, 0.16);
    ctx.lineWidth = 1;
    ctx.stroke();
    /* 标签放东北 45°：正东/正北正好压在罗盘刻度与十字线上 */
    var label = String(Math.round(value * 10) / 10) + ' km';
    haloText(ctx, label,
      view.cx + px * Math.SQRT1_2,
      view.cy - px * Math.SQRT1_2,
      rgba(scan, 0.75), font);
  });
}

function drawCrosshair(ctx, scan) {
  var view = state.view;
  ctx.save();
  ctx.strokeStyle = rgba(scan, 0.12);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(view.cx - view.radiusPx, view.cy);
  ctx.lineTo(view.cx + view.radiusPx, view.cy);
  ctx.moveTo(view.cx, view.cy - view.radiusPx);
  ctx.lineTo(view.cx, view.cy + view.radiusPx);
  ctx.stroke();
  ctx.restore();
}

function drawSweep(ctx, scan, now) {
  var view = state.view;
  var bearing = ((now % SWEEP_PERIOD_MS) / SWEEP_PERIOD_MS) * 360;
  var stepDeg = 360 / (SWEEP_TAIL_STEPS * 2.2);
  /* 余晖用一串递减的扇形叠出来，而不是画一张渐变：
   * createConicGradient 在旧 WebView 上还没有，扇形在任何地方都一样。 */
  for (var i = 0; i < SWEEP_TAIL_STEPS; i += 1) {
    var tail = (1 - i / SWEEP_TAIL_STEPS);
    var from = bearing - (i + 1) * stepDeg;
    var to = bearing - i * stepDeg;
    ctx.beginPath();
    ctx.moveTo(view.cx, view.cy);
    ctx.arc(view.cx, view.cy, view.radiusPx,
      toRad(from - 90), toRad(to - 90));
    ctx.closePath();
    ctx.fillStyle = rgba(scan, 0.05 * tail * tail + 0.012);
    ctx.fill();
  }
  var leading = toRad(bearing - 90);
  var x = view.cx + view.radiusPx * Math.cos(leading);
  var y = view.cy + view.radiusPx * Math.sin(leading);
  ctx.save();
  ctx.strokeStyle = rgba(scan, 0.85);
  ctx.lineWidth = 1.6;
  ctx.shadowColor = rgba(scan, 0.9);
  ctx.shadowBlur = 6;
  ctx.beginPath();
  ctx.moveTo(view.cx, view.cy);
  ctx.lineTo(x, y);
  ctx.stroke();
  ctx.restore();
}

function drawAircraftIcon(ctx, x, y, heading, scale) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(toRad(heading));
  ctx.scale(scale, scale);
  ctx.beginPath();
  ctx.moveTo(0, -9);
  ctx.lineTo(2.2, -2);
  ctx.lineTo(8, 3);
  ctx.lineTo(8, 5);
  ctx.lineTo(1.6, 2.6);
  ctx.lineTo(1.4, 7);
  ctx.lineTo(3.2, 8.8);
  ctx.lineTo(3.2, 10);
  ctx.lineTo(0, 9);
  ctx.lineTo(-3.2, 10);
  ctx.lineTo(-3.2, 8.8);
  ctx.lineTo(-1.4, 7);
  ctx.lineTo(-1.6, 2.6);
  ctx.lineTo(-8, 5);
  ctx.lineTo(-8, 3);
  ctx.lineTo(-2.2, -2);
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function targetColor(flight, selected) {
  if (selected) {
    return '#5AFFC2';
  }
  if (flight.flags && flight.flags.special) {
    return '#5AFFC2';
  }
  if (flight.on_ground) {
    return '#4A5D57';
  }
  if (flight.flags && flight.flags.widebody) {
    return '#E8C547';
  }
  return '#C8D6D0';
}

function drawTargets(ctx, scan, now) {
  var view = state.view;
  var font = '11px ' + MONO_FONT;
  var visible = [];

  /* 第一遍只画图标。标签单独一遍画，因为要做避让：
   * 逐个目标"画图标 + 画标签"会让先画到的标签占住位置，
   * 后画的再判断重叠就已经晚了。 */
  state.tracks.forEach(function (track, icao) {
    var flight = track.flight;
    var position = trackScreen(track, now);
    var dx = position.x - view.cx;
    var dy = position.y - view.cy;
    if (Math.hypot(dx, dy) > view.radiusPx + 12) {
      return; /* 出了量程圈就不该再画，否则会浮在盘外的罗盘刻度上 */
    }
    var selected = state.selected === icao;
    var color = targetColor(flight, selected);
    var heading = (typeof flight.track === 'number')
      ? flight.track
      : initialBearing(track.fromLat, track.fromLon, track.toLat, track.toLon);

    ctx.save();
    ctx.fillStyle = color;
    ctx.strokeStyle = 'rgba(11, 14, 15, 0.8)';
    ctx.lineWidth = 1;
    ctx.shadowColor = rgba(color, selected ? 0.9 : 0.35);
    ctx.shadowBlur = selected ? 10 : 4;
    drawAircraftIcon(ctx, position.x, position.y, heading, selected ? 1.05 : 0.82);
    ctx.restore();

    if (selected) {
      ctx.beginPath();
      ctx.arc(position.x, position.y, 17, 0, TAU);
      ctx.strokeStyle = rgba('#5AFFC2', 0.85);
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    visible.push({
      icao: icao,
      flight: flight,
      x: position.x,
      y: position.y,
      color: color,
      selected: selected
    });
  });

  /* 第二遍画标签：选中项永远优先，其余按距离由近到远（服务端已排好序）。
   * 窄屏的盘小，能放下的标签很有限，所以要设上限并做矩形避让——
   * 否则 30 架飞机挤在一起，标签会糊成一团完全没法读。 */
  var limit = isMobileLayout() ? 12 : 26;
  var placed = [];
  visible.sort(function (a, b) {
    if (a.selected !== b.selected) { return a.selected ? -1 : 1; }
    return (a.flight.distance_km || 0) - (b.flight.distance_km || 0);
  });

  var drawn = 0;
  for (var i = 0; i < visible.length; i++) {
    var item = visible[i];
    /* 标签只给空中目标：地面目标在机场附近会堆成一团糊字 */
    if (item.flight.on_ground && !item.selected) {
      continue;
    }
    if (drawn >= limit && !item.selected) {
      break;
    }
    var label = flightName(item.flight);
    var short = (item.flight.display || {}).altitude_short || '';
    var width = Math.max(ctx.measureText(label).width,
      short ? ctx.measureText(short).width : 0) + 8;
    var box = {
      x: item.x - width / 2,
      y: item.y + 11,
      w: width,
      h: short ? 26 : 13
    };
    var blocked = placed.some(function (other) {
      return box.x < other.x + other.w && other.x < box.x + box.w &&
        box.y < other.y + other.h && other.y < box.y + box.h;
    });
    if (blocked) {
      continue;
    }
    placed.push(box);
    drawn += 1;
    haloText(ctx, label, item.x, item.y + 21, item.color, font);
    if (short) {
      haloText(ctx, short, item.x, item.y + 32, rgba(scan, 0.8), font);
    }
  }
}

function drawOuterRings(ctx, scan) {
  var view = state.view;
  ctx.beginPath();
  ctx.arc(view.cx, view.cy, view.radiusPx, 0, TAU);
  ctx.strokeStyle = rgba(scan, 0.55);
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(view.cx, view.cy, view.radiusPx + 4, 0, TAU);
  ctx.strokeStyle = rgba(scan, 0.18);
  ctx.lineWidth = 1;
  ctx.stroke();
}

function drawCompass(ctx, scan) {
  var view = state.view;
  ctx.save();
  for (var bearing = 0; bearing < 360; bearing += 5) {
    var major = bearing % 30 === 0;
    var medium = bearing % 10 === 0;
    var length = major ? 11 : (medium ? 7 : 4);
    var angle = toRad(bearing - 90);
    var inner = view.radiusPx + 4;
    ctx.beginPath();
    ctx.moveTo(view.cx + inner * Math.cos(angle), view.cy + inner * Math.sin(angle));
    ctx.lineTo(view.cx + (inner + length) * Math.cos(angle),
      view.cy + (inner + length) * Math.sin(angle));
    ctx.strokeStyle = rgba(scan, major ? 0.6 : 0.28);
    ctx.lineWidth = major ? 1.4 : 1;
    ctx.stroke();
  }
  ctx.restore();

  var letters = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' };
  var font = 'bold 12px ' + MONO_FONT;
  Object.keys(letters).forEach(function (key) {
    var bearing = Number(key);
    var angle = toRad(bearing - 90);
    var distance = view.radiusPx + 18;
    haloText(ctx, letters[key],
      view.cx + distance * Math.cos(angle),
      view.cy + distance * Math.sin(angle),
      bearing === 0 ? rgba(scan, 0.95) : 'rgba(110, 127, 122, 0.95)', font);
  });
}

function drawCentre(ctx, scan) {
  var view = state.view;
  ctx.beginPath();
  ctx.arc(view.cx, view.cy, 6, 0, TAU);
  ctx.strokeStyle = rgba(scan, 0.7);
  ctx.lineWidth = 1.4;
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(view.cx, view.cy, 2.2, 0, TAU);
  ctx.fillStyle = rgba(scan, 0.95);
  ctx.fill();
}

function drawStatusLine(ctx, scan) {
  /* 状态信息已移到 DOM（见 renderStatus），canvas 里不再画。
   * 保留空实现是为了让 draw() 的调用顺序保持稳定。 */
}

function frameLoop(now) {
  if (state.running && !dom.app.hidden) {
    draw(now);
  }
  window.requestAnimationFrame(frameLoop);
}

// ====================== 榜单 ======================

function renderList() {
  var flights = state.frame.flights.slice(0, LIST_ROWS);
  /* 每轮轮询都重建列表，先把滚动位置存下来：不然列表每 10 秒自己跳回顶部 */
  var scrollTop = dom.panelBody.scrollTop;
  dom.flightList.textContent = '';
  dom.listCount.textContent = state.frame.flights.length
    ? (state.frame.flights.length + ' 架在范围内')
    : '—';
  dom.listEmpty.hidden = flights.length > 0;
  if (!flights.length) {
    dom.listEmpty.textContent = state.localNotice ||
      state.frame.notice || '当前范围内没有航班';
  }

  flights.forEach(function (flight) {
    var icao = String(flight.icao24 || '').toLowerCase();
    var display = flight.display || {};
    var flags = flight.flags || {};
    var row = el('button', 'flight-row');
    row.type = 'button';
    row.setAttribute('aria-current', state.selected === icao ? 'true' : 'false');

    var head = el('div', 'row-line');
    var left = el('span');
    left.appendChild(el('span', 'row-call', flightName(flight)));
    if (flags.widebody) {
      left.appendChild(el('span', 'tag tag-widebody', '宽体'));
    }
    if (flags.special) {
      left.appendChild(el('span', 'tag tag-special', '特殊'));
    }
    if (flight.on_ground) {
      left.appendChild(el('span', 'tag tag-ground', '地面'));
    }
    head.appendChild(left);
    head.appendChild(el('span', 'row-right', display.distance || ''));
    row.appendChild(head);

    var route = el('div', 'row-line');
    route.appendChild(el('span', 'row-sub', display.route_label || '航路未知'));
    route.appendChild(el('span', 'row-right', display.eta || ''));
    row.appendChild(route);

    var model = [];
    if (display.model) {
      model.push(display.model);
    }
    if (display.airline) {
      model.push(display.airline);
    }
    row.appendChild(el('div', 'row-sub', model.length ? model.join(' · ') : '机型未知'));

    row.addEventListener('click', function () {
      if (state.selected === icao) {
        openDetail();
      } else {
        selectTrack(icao);
      }
    });
    var item = el('li');
    item.appendChild(row);
    dom.flightList.appendChild(item);
  });
  dom.panelBody.scrollTop = scrollTop;
}

function selectTrack(icao) {
  state.selected = icao;
  renderList();
  if (isMobileLayout()) {
    expandSheet();
  }
}

function handleTap(x, y) {
  var now = performance.now();
  var best = null;
  var bestDistance = Infinity;
  state.tracks.forEach(function (track, icao) {
    var point = trackScreen(track, now);
    var distance = Math.hypot(point.x - x, point.y - y);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = icao;
    }
  });
  /* 44 CSS px 的命中半径：点得够近就吸附到最近的一架；点在已选中的那架上则进详情。 */
  if (best !== null && bestDistance <= TAP_RADIUS_PX) {
    if (best === state.selected) {
      openDetail();
    } else {
      selectTrack(best);
    }
    return;
  }
  if (state.selected) {
    state.selected = null;
    renderList();
  }
}

// ====================== 详情 ======================

function setTab(name) {
  if (!dom.tab[name]) {
    return;
  }
  state.tab = name;
  ['list', 'detail', 'settings'].forEach(function (key) {
    dom.view[key].hidden = key !== name;
    dom.tab[key].setAttribute('aria-selected', key === name ? 'true' : 'false');
  });
  if (name !== 'list' && isMobileLayout()) {
    expandSheet();
  }
  if (name === 'detail' && state.selected) {
    loadDetail(state.selected, !!state.detail);
  }
}

function openDetail() {
  setTab('detail');
  if (state.selected) {
    loadDetail(state.selected, !!state.detail);
  }
}

function loadDetail(icao, silent) {
  if (!icao || !state.center) {
    return;
  }
  if (state.detailLoading) {
    return;
  }
  state.detailLoading = true;
  if (!silent) {
    state.detail = null;
    renderDetail();
  }
  var query = queryFor(state.center.lat, state.center.lon, currentRadiusKm());
  apiJson('/api/flight/' + encodeURIComponent(icao) + '?' + query).then(function (data) {
    state.detailLoading = false;
    state.detail = data;
    renderDetail();
  }, function (error) {
    state.detailLoading = false;
    if (error && error.status === 401) {
      showLogin('登录已过期，请重新登录');
      return;
    }
    state.detail = { error: (error && error.message) || '详情获取失败' };
    renderDetail();
  });
}

function addDetailRow(list, label, value) {
  if (value === undefined || value === null || value === '') {
    return;
  }
  list.appendChild(el('dt', null, label));
  list.appendChild(el('dd', null, String(value)));
}

function renderDetail() {
  var selected = state.selected;
  dom.detailEmpty.hidden = !!selected;
  dom.detailBody.hidden = !selected;
  dom.detailBody.textContent = '';
  if (!selected) {
    return;
  }
  var detail = state.detail;
  if (!detail) {
    dom.detailBody.appendChild(el('p', 'panel-note', '加载中…'));
    return;
  }
  if (detail.error) {
    dom.detailBody.appendChild(el('p', 'empty-state', detail.error));
    return;
  }
  if (!detail.flight) {
    /* flight: null 是常态（航班飞出了量程圈），不是错误 */
    dom.detailBody.appendChild(el('p', 'empty-state',
      detail.reason || '该航班已不在当前雷达范围内'));
    return;
  }

  var flight = detail.flight;
  var display = flight.display || {};
  var info = flight.info || {};
  var flags = flight.flags || {};

  dom.detailBody.appendChild(el('h3', 'detail-call', flightName(flight)));
  var subtitle = [];
  if (display.model) {
    subtitle.push(display.model);
  }
  if (display.airline) {
    subtitle.push(display.airline);
  }
  dom.detailBody.appendChild(el('p', 'detail-sub',
    subtitle.join(' · ') || String(flight.icao24 || '').toUpperCase()));

  var grid = el('dl', 'detail-grid');
  addDetailRow(grid, '航线', display.route_label);
  addDetailRow(grid, '预计到达', display.eta);
  addDetailRow(grid, '阶段', display.phase);
  addDetailRow(grid, '高度', display.altitude);
  addDetailRow(grid, '地速', display.speed);
  addDetailRow(grid, '航向', display.heading);
  addDetailRow(grid, '升降', display.vertical);
  addDetailRow(grid, '距离', display.distance);
  addDetailRow(grid, '方位', display.bearing);
  addDetailRow(grid, '注册号', display.registration);
  addDetailRow(grid, '机型代码', info.type_code);
  addDetailRow(grid, '制造商', info.manufacturer);
  addDetailRow(grid, '应答机', flight.squawk);
  addDetailRow(grid, '国家/地区', flight.origin_country);
  addDetailRow(grid, '目的地时差', display.destination_offset);
  addDetailRow(grid, '备注', flags.special_note);
  if (detail.stale) {
    addDetailRow(grid, '数据', '延迟');
  }
  dom.detailBody.appendChild(grid);

  var actions = el('div', 'detail-actions');
  var close = el('button', 'btn', '取消选择');
  close.type = 'button';
  close.addEventListener('click', function () {
    state.selected = null;
    state.detail = null;
    renderList();
    setTab('list');
  });
  actions.appendChild(close);
  if (info.photo_url) {
    /* 只给链接不做 <img>：CSP 的 img-src 是 'self' data:，外站图必被拦 */
    var link = el('a', 'detail-link', '查看照片');
    link.href = info.photo_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    actions.appendChild(link);
  }
  dom.detailBody.appendChild(actions);
}

// ====================== 设置 ======================

function settingsGroup(title, children) {
  var section = el('section', 'settings-group');
  section.appendChild(el('p', 'settings-legend', title));
  children.forEach(function (child) {
    section.appendChild(child);
  });
  return section;
}

function settingRow(nameText, control, valueNode) {
  var row = el('div', 'setting-row');
  var stack = el('div', 'setting-stack');
  stack.appendChild(el('span', 'setting-name', nameText));
  if (valueNode) {
    stack.appendChild(valueNode);
  }
  row.appendChild(stack);
  var cell = el('div', 'setting-control');
  cell.appendChild(control);
  row.appendChild(cell);
  return row;
}

function numberControl(key) {
  var config = state.config;
  var spec = NUMERIC_RANGES[key];
  var input = el('input', 'text-input');
  input.type = 'number';
  input.inputMode = 'numeric';
  input.min = String(spec.min);
  input.max = String(spec.max);
  input.step = String(spec.step);
  input.value = String(config[key]);
  input.addEventListener('change', function () {
    var value = Number(input.value);
    if (!isFinite(value)) {
      input.value = String(state.config[key]);
      return;
    }
    var patch = {};
    patch[key] = Math.round(clamp(value, spec.min, spec.max));
    saveConfig(patch);
  });
  return input;
}

function buildSettings() {
  var config = state.config;
  if (!config) {
    return;
  }
  settingsControls = {};
  dom.settingsBody.textContent = '';

  // ---- 雷达 ----
  var rangeInput = el('input');
  rangeInput.type = 'range';
  rangeInput.min = '0';
  rangeInput.max = String(RANGE_STOPS.length - 1);
  rangeInput.step = '1';
  rangeInput.value = String(state.radiusIndex);
  rangeInput.setAttribute('aria-label', '量程档位');
  var rangeValue = el('span', 'setting-value', currentRadiusKm() + ' km');
  rangeInput.addEventListener('input', function () {
    setRangeIndex(Number(rangeInput.value), false);
  });
  rangeInput.addEventListener('change', function () {
    setRangeIndex(Number(rangeInput.value), true);
  });
  settingsControls.range = rangeInput;
  settingsControls.rangeValue = rangeValue;

  var colorInput = el('input');
  colorInput.type = 'color';
  colorInput.value = config.scan_color;
  colorInput.setAttribute('aria-label', '扫描线颜色');
  colorInput.addEventListener('input', function () {
    /* 先本地生效再落盘：取色器会连发几十次 input，逐次 PUT 没必要 */
    document.documentElement.style.setProperty('--scan', colorInput.value);
    settingsControls.colorValue.textContent = String(colorInput.value).toUpperCase();
    debouncedSave({ scan_color: String(colorInput.value).toUpperCase() });
  });
  settingsControls.colorValue = el('span', 'setting-value', config.scan_color);

  dom.settingsBody.appendChild(settingsGroup('雷达', [
    settingRow('量程', rangeInput, rangeValue),
    settingRow('扫描色', colorInput, settingsControls.colorValue)
  ]));

  // ---- 数据 ----
  var sourceSelect = el('select', 'text-input');
  sourceSelect.setAttribute('aria-label', '数据源');
  /* 选项来自 /api/state 的 sources 数组，不写死列表：服务端增删源这里自动跟上 */
  state.sources.forEach(function (id) {
    var option = el('option', null, sourceLabel(id));
    option.value = id;
    if (id === config.source) {
      option.selected = true;
    }
    sourceSelect.appendChild(option);
  });
  sourceSelect.addEventListener('change', function () {
    saveConfig({ source: sourceSelect.value });
  });

  var unitSelect = el('select', 'text-input');
  unitSelect.setAttribute('aria-label', '高度单位');
  [['m', '米 (m)'], ['ft', '英尺 (ft)']].forEach(function (pair) {
    var option = el('option', null, pair[1]);
    option.value = pair[0];
    if (pair[0] === config.altitude_unit) {
      option.selected = true;
    }
    unitSelect.appendChild(option);
  });
  unitSelect.addEventListener('change', function () {
    saveConfig({ altitude_unit: unitSelect.value });
  });

  dom.settingsBody.appendChild(settingsGroup('数据', [
    settingRow('数据源', sourceSelect, el('span', 'setting-value', sourceLabel(config.source))),
    settingRow('刷新间隔', numberControl('refresh_sec'),
      el('span', 'setting-value', config.refresh_sec + ' 秒')),
    settingRow('最多航班', numberControl('max_flights'),
      el('span', 'setting-value', config.max_flights + ' 架')),
    settingRow('高度单位', unitSelect,
      el('span', 'setting-value', config.altitude_unit === 'ft' ? '英尺' : '米')),
    settingRow('显示地面目标', groundToggle(config),
      el('span', 'setting-value', config.show_ground ? '开' : '关'))
  ]));

  // ---- 底图叠加 ----
  var layerRows = [];
  Object.keys(config).forEach(function (key) {
    /* 开关全从配置对象里长出来：服务端加一个 show_* 键，这里自动多一行 */
    if (key.indexOf('show_') !== 0 || key === 'show_ground') {
      return;
    }
    layerRows.push(settingRow(TOGGLE_LABELS[key] || key, layerToggle(config, key),
      el('span', 'setting-value', config[key] ? '开' : '关')));
  });
  dom.basemapNote = el('p', 'panel-note', '');
  layerRows.push(dom.basemapNote);
  dom.settingsBody.appendChild(settingsGroup('底图叠加', layerRows));

  // ---- 定位 ----
  var locationStack = el('div', 'setting-stack');
  locationStack.appendChild(el('span', 'setting-name', '当前定位：' + state.locationLabel));
  if (state.center) {
    locationStack.appendChild(el('span', 'setting-value',
      state.center.lat.toFixed(4) + ', ' + state.center.lon.toFixed(4)));
  }
  var manual = manualFromState({ config: state.config });
  if (manual) {
    locationStack.appendChild(el('span', 'setting-value',
      '配置手动坐标 ' + manual.lat.toFixed(4) + ', ' + manual.lon.toFixed(4)));
  }
  var relocalize = el('button', 'btn', '重新定位');
  relocalize.type = 'button';
  relocalize.addEventListener('click', function () {
    resolveLocation();
    buildSettings();
  });
  dom.settingsBody.appendChild(settingsGroup('定位', [
    locationStack,
    settingRow('重新定位', relocalize, null)
  ]));

  renderBasemapNote();
  renderRangeLabel();
}

function groundToggle(config) {
  var input = el('input');
  input.type = 'checkbox';
  input.checked = !!config.show_ground;
  input.setAttribute('aria-label', '显示地面目标');
  input.addEventListener('change', function () {
    saveConfig({ show_ground: input.checked });
  });
  return input;
}

function layerToggle(config, key) {
  var input = el('input');
  input.type = 'checkbox';
  input.checked = !!config[key];
  input.setAttribute('aria-label', TOGGLE_LABELS[key] || key);
  input.addEventListener('change', function () {
    var patch = {};
    patch[key] = input.checked;
    saveConfig(patch);
  });
  return input;
}

/** 写配置。服务端会把越界值夹回去，所以默认按返回值重建控件，让界面等于真实生效值。 */
function saveConfig(patch, rebuild) {
  if (!state.config) {
    return Promise.resolve();
  }
  return apiJson('/api/config', {
    method: 'PUT',
    body: JSON.stringify(patch)
  }).then(function (data) {
    applyConfig(data.config);
    reconcileConfig(patch);
    if (rebuild !== false) {
      buildSettings();
    }
    renderCooling();
  }, function (error) {
    handleError(error);
  });
}

function reconcileConfig(patch) {
  var layersTouched = false;
  LAYER_KEYS.forEach(function (pair) {
    if (patch[pair[1]] !== undefined) {
      layersTouched = true;
    }
  });
  if (layersTouched || patch.radius_km !== undefined) {
    state.basemap.key = '';
    state.basemap.failedAt = 0;
    fetchBasemap();
  }
  if (patch.refresh_sec !== undefined) {
    startPolling();
  }
  var refetch = ['radius_km', 'source', 'show_ground', 'altitude_unit',
    'max_flights', 'stale_limit_sec'];
  for (var i = 0; i < refetch.length; i += 1) {
    if (patch[refetch[i]] !== undefined) {
      pollFlights();
      return;
    }
  }
}

// ====================== 量程 ======================

var pollDebounceTimer = 0;
var saveDebounceTimer = 0;

function debouncedPoll() {
  window.clearTimeout(pollDebounceTimer);
  pollDebounceTimer = window.setTimeout(function () {
    pollFlights();
  }, 260);
}

function debouncedSave(patch) {
  window.clearTimeout(saveDebounceTimer);
  saveDebounceTimer = window.setTimeout(function () {
    saveConfig(patch, false);
  }, 350);
}

/**
 * 切换量程档位。
 *
 * ``persist`` 为假时只改本地（拖动过程中），为真时写回配置。
 * 拖动中不写盘的原因：滑块 input 事件一秒能发几十次，逐次 PUT 会打满限流。
 */
function setRangeIndex(index, persist) {
  var clamped = clamp(Math.round(index), 0, RANGE_STOPS.length - 1);
  var changed = clamped !== state.radiusIndex;
  state.radiusIndex = clamped;
  dom.rangeSlider.value = String(clamped);
  if (settingsControls.range) {
    settingsControls.range.value = String(clamped);
  }
  if (settingsControls.rangeValue) {
    settingsControls.rangeValue.textContent = currentRadiusKm() + ' km';
  }
  renderRangeLabel();
  if (!changed && !persist) {
    return;
  }
  state.basemap.key = '';
  state.basemap.failedAt = 0;
  fetchBasemap();
  if (persist) {
    saveConfig({ radius_km: currentRadiusKm() }, false);
  } else {
    debouncedPoll();
  }
}

// ====================== 触屏交互 ======================

function initRadarInput() {
  var pointers = new Map();
  var pinch = null;
  var tap = null;

  dom.radar.addEventListener('pointerdown', function (event) {
    dom.radar.setPointerCapture(event.pointerId);
    var rect = dom.radar.getBoundingClientRect();
    pointers.set(event.pointerId, {
      x: event.clientX - rect.left,
      y: event.clientY - rect.top
    });
    var list = [];
    pointers.forEach(function (value) {
      list.push(value);
    });
    if (pointers.size === 1) {
      tap = { x: list[0].x, y: list[0].y };
    } else {
      tap = null;
    }
    if (pointers.size === 2) {
      var dx = list[0].x - list[1].x;
      var dy = list[0].y - list[1].y;
      pinch = {
        distance: Math.max(1, Math.hypot(dx, dy)),
        radius: currentRadiusKm()
      };
    }
  });

  dom.radar.addEventListener('pointermove', function (event) {
    if (!pointers.has(event.pointerId)) {
      return;
    }
    var rect = dom.radar.getBoundingClientRect();
    var current = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    pointers.set(event.pointerId, current);

    var list = [];
    pointers.forEach(function (value) {
      list.push(value);
    });

    if (pinch && pointers.size >= 2) {
      var dx = list[0].x - list[1].x;
      var dy = list[0].y - list[1].y;
      var distance = Math.max(1, Math.hypot(dx, dy));
      /* 捏合直接换算出新的半径（张开 = 缩小半径 = 放大视角），再落到最近档位 */
      var radius = pinch.radius * (pinch.distance / distance);
      setRangeIndex(nearestRangeIndex(radius), false);
      return;
    }
    if (tap && Math.hypot(current.x - tap.x, current.y - tap.y) > TAP_SLOP_PX) {
      tap = null;
    }
  });

  function endPointer(event) {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) {
      pinch = null;
    }
    if (pointers.size > 0) {
      return;
    }
    if (tap) {
      if (state.localNotice) {
        state.localNotice = '';
        renderNotices();
      }
      handleTap(tap.x, tap.y);
    }
    tap = null;
  }

  dom.radar.addEventListener('pointerup', endPointer);
  dom.radar.addEventListener('pointercancel', endPointer);

  /* 滚轮是桌面端的便利，不是唯一入口：常驻 −/+ 与滑块才是。 */
  dom.radar.addEventListener('wheel', function (event) {
    event.preventDefault();
    if (Math.abs(event.deltaY) < 1) {
      return;
    }
    setRangeIndex(state.radiusIndex + (event.deltaY > 0 ? 1 : -1), false);
    /* 滚轮一格一个事件，逐格 PUT 会打满限流，攒一下再写 */
    debouncedSave({ radius_km: currentRadiusKm() });
  }, { passive: false });
}

function sheetSnaps() {
  var peek = 116;
  var half = Math.round(window.innerHeight * 0.46);
  return {
    peek: peek,
    half: half,
    full: Math.max(half, window.innerHeight - 76)
  };
}

function setSheetHeight(px, animate) {
  var value = clamp(Math.round(px), 96, Math.max(160, window.innerHeight - 60));
  state.sheetHeight = value;
  document.documentElement.style.setProperty('--sheet-h', value + 'px');
  /* 拖动中要跟手（关掉过渡），松手后的吸附才用过渡 */
  dom.panel.classList.toggle('dragging', !animate);
  computeDish();
}

function snapSheet() {
  var snaps = sheetSnaps();
  var candidates = [snaps.peek, snaps.half, snaps.full];
  var best = candidates[0];
  candidates.forEach(function (value) {
    if (Math.abs(value - state.sheetHeight) < Math.abs(best - state.sheetHeight)) {
      best = value;
    }
  });
  setSheetHeight(best, true);
}

function expandSheet() {
  var snaps = sheetSnaps();
  if (state.sheetHeight < snaps.half) {
    setSheetHeight(snaps.half, true);
  }
}

function initSheet() {
  var drag = null;

  dom.sheetHandle.addEventListener('pointerdown', function (event) {
    drag = {
      y: event.clientY,
      height: dom.panel.getBoundingClientRect().height,
      moved: false
    };
    dom.sheetHandle.setPointerCapture(event.pointerId);
  });

  dom.sheetHandle.addEventListener('pointermove', function (event) {
    if (!drag) {
      return;
    }
    var dy = drag.y - event.clientY;
    if (Math.abs(dy) > 4) {
      drag.moved = true;
    }
    setSheetHeight(drag.height + dy, false);
  });

  dom.sheetHandle.addEventListener('pointerup', function () {
    if (!drag) {
      return;
    }
    var moved = drag.moved;
    drag = null;
    if (moved) {
      snapSheet();
    } else {
      toggleSheet();
    }
  });

  dom.sheetHandle.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggleSheet();
    }
  });
}

function toggleSheet() {
  var snaps = sheetSnaps();
  setSheetHeight(state.sheetHeight > snaps.peek + 12 ? snaps.peek : snaps.half, true);
}

function syncLayout() {
  if (isMobileLayout()) {
    var snaps = sheetSnaps();
    setSheetHeight(state.sheetHeight || snaps.half, true);
  } else {
    state.sheetHeight = 0;
  }
  resizeCanvas();
}

function initResize() {
  if (window.ResizeObserver) {
    var observer = new ResizeObserver(function () {
      resizeCanvas();
    });
    observer.observe(dom.radar.parentNode);
  }
  window.addEventListener('resize', syncLayout);
  window.addEventListener('orientationchange', syncLayout);
}

// ====================== 启动 ======================

function cacheDom() {
  dom.login = $('login-screen');
  dom.loginForm = $('login-form');
  dom.password = $('login-password');
  dom.loginButton = $('login-submit');
  dom.loginError = $('login-error');
  dom.app = $('app-screen');
  dom.radar = $('radar');
  dom.context = dom.radar.getContext('2d');
  dom.locationBadge = $('location-badge');
  dom.staleBadge = $('stale-badge');
  dom.statusLine = $('status-line');
  dom.noticeBar = $('notice-bar');
  dom.rangeOut = $('range-out');
  dom.rangeIn = $('range-in');
  dom.rangeSlider = $('range-slider');
  dom.rangeLabel = $('range-label');
  dom.recenter = $('recenter');
  dom.logout = $('logout');
  dom.panel = $('panel');
  dom.panelBody = document.querySelector('.panel-body');
  dom.sheetHandle = $('sheet-handle');
  dom.tab = { list: $('tab-list'), detail: $('tab-detail'), settings: $('tab-settings') };
  dom.view = {
    list: $('view-list'),
    detail: $('view-detail'),
    settings: $('view-settings')
  };
  dom.flightList = $('flight-list');
  dom.listEmpty = $('list-empty');
  dom.listCount = $('list-count');
  dom.detailEmpty = $('detail-empty');
  dom.detailBody = $('detail-body');
  dom.settingsBody = $('settings-body');
  dom.coolingNote = $('cooling-note');
  dom.basemapNote = null;
}

function init() {
  cacheDom();
  dom.loginForm.addEventListener('submit', submitLogin);
  dom.logout.addEventListener('click', logout);
  dom.recenter.addEventListener('click', function () {
    resolveLocation();
  });
  dom.rangeOut.addEventListener('click', function () {
    setRangeIndex(state.radiusIndex - 1, true);
  });
  dom.rangeIn.addEventListener('click', function () {
    setRangeIndex(state.radiusIndex + 1, true);
  });
  dom.rangeSlider.addEventListener('input', function () {
    setRangeIndex(Number(dom.rangeSlider.value), false);
  });
  dom.rangeSlider.addEventListener('change', function () {
    setRangeIndex(Number(dom.rangeSlider.value), true);
  });
  dom.tab.list.addEventListener('click', function () {
    setTab('list');
  });
  dom.tab.detail.addEventListener('click', function () {
    setTab('detail');
  });
  dom.tab.settings.addEventListener('click', function () {
    setTab('settings');
  });

  initRadarInput();
  initSheet();
  initResize();
  renderRangeLabel();
  syncLayout();
  window.requestAnimationFrame(frameLoop);

  /* 先探一次会话：Cookie 还有效就直接进主界面，不该让用户再输一次口令。 */
  apiJson('/api/state').then(function () {
    return boot();
  }, function () {
    showLogin('');
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
