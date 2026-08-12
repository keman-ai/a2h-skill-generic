/* desk UI 页面脚本。刻意保持薄：长轮询换片段、转发点击、复制联系方式、
   图片比例钳制 —— 没有第二套模板（HTML 全部由服务端渲染，见 deskui_pages.py 头注）。
   通过 CSP 外置加载；启动状态从 #boot JSON 数据岛读（CSP 下不执行、无内联脚本）。
   0.38.1 起页面没有任何会惊动 agent 的动作 —— 事件流/忙碌提示条的代码随之删除。 */
(function () {
  'use strict';

  var boot = JSON.parse(document.getElementById('boot').textContent || '{}');
  var revision = boot.revision || 0;
  var currentView = boot.view || 'search';
  var toastTimer = null;

  var params = new URLSearchParams(location.search);
  var TOKEN = params.get('k') || '';

  function api(path, options) {
    var sep = path.indexOf('?') >= 0 ? '&' : '?';
    return fetch(path + sep + 'k=' + encodeURIComponent(TOKEN), options);
  }

  function setLive(on) {
    var dot = document.getElementById('live');
    if (dot) { dot.className = on ? 'dot' : 'dot off'; }
  }

  function showToast(message) {
    var toast = document.getElementById('toast');
    if (!toast) { return; }
    toast.textContent = message;
    toast.hidden = false;
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { toast.hidden = true; }, 2500);
  }

  /* ── 图片比例钳制（对 lib/imageRatio.ts 的移植）：3:4 ~ 4:3 之间用原比例，
     超界钳到边界裁切；未知时 CSS 里的 4:3 兜底 ── */
  function clampRatio(img) {
    if (!img.naturalWidth || !img.naturalHeight) { return; }
    var ratio = img.naturalWidth / img.naturalHeight;
    var clamped = Math.min(Math.max(ratio, 0.75), 4 / 3);
    var box = img.parentElement;
    if (box) { box.style.aspectRatio = String(clamped); }
  }
  function watchRatios(root) {
    root.querySelectorAll('img[data-ratio]').forEach(function (img) {
      if (img.complete) { clampRatio(img); }
      else { img.addEventListener('load', function () { clampRatio(img); }, { once: true }); }
    });
  }

  function applyState(state) {
    if (!state || state.revision <= revision) { return; }
    var previousRevision = revision;
    var sameView = currentView === state.view;
    revision = state.revision;
    /* 局部补丁只对它的直接前序状态安全；若长轮询合并跳过了中间 revision，
       必须用完整 HTML 追平，不能把新弹层贴到旧详情正文上。 */
    if (sameView && state.update_scope === 'overlay' &&
        state.revision === previousRevision + 1) {
      var overlay = document.getElementById('overlay-root');
      if (overlay) { overlay.innerHTML = state.overlay_html || ''; return; }
    }
    /* 切屏或详情正文更新才整段换 HTML；滚动位置用前后快照兜住。 */
    var y = window.scrollY;
    var hadModal = !!document.querySelector('.modal-backdrop');
    var view = document.getElementById('view');
    view.innerHTML = state.html;
    currentView = state.view;
    watchRatios(view);
    var hasModal = !!view.querySelector('.modal-backdrop');
    if (hadModal !== hasModal) { window.scrollTo(0, y); }
    else { window.scrollTo(0, 0); }
  }

  function submit(action, source) {
    if (source) {
      source.classList.add('is-pending');
      source.setAttribute('aria-busy', 'true');
    }
    function clearPending() {
      if (!source) { return; }
      source.classList.remove('is-pending');
      source.removeAttribute('aria-busy');
    }
    api('/api/human-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, expected_revision: revision })
    }).then(function (response) {
      response.json().then(function (payload) {
        clearPending();
        if (response.ok) {
          /* 动作响应直接带新状态，切屏不必再等那条长轮询往返。 */
          applyState(payload.state);
          return;
        }
        /* 409 = 状态刚被推进，长轮询马上带来新状态，不当错误惊动人 */
        if (response.status !== 409) { showToast(payload.error || ('操作失败 ' + response.status)); }
      }).catch(function () { clearPending(); });
    }).catch(function () { clearPending(); setLive(false); });
  }

  /* ── 点击转发：一切动作都从服务端渲染的 data-act 里取，页面自己造不出动作。
     背板（data-backdrop）只在点它本体时触发 —— 弹层卡片里的点击不算 ── */
  document.addEventListener('click', function (event) {
    var target = event.target.closest('[data-act]');
    if (!target) { return; }
    if (target.hasAttribute('data-backdrop') && event.target.closest('.modal-card')) { return; }
    submit(JSON.parse(target.getAttribute('data-act')), target);
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' && event.key !== ' ') { return; }
    var target = event.target.closest('[role="button"][data-act]');
    if (!target) { return; }
    event.preventDefault();
    submit(JSON.parse(target.getAttribute('data-act')), target);
  });

  /* 复制联系方式：页面唯一的本地行为（不产生回传动作）。按钮短暂变「已复制」。 */
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy]');
    if (!button) { return; }
    var value = button.getAttribute('data-copy');
    var done = function () {
      var original = button.textContent;
      button.textContent = '已复制';
      setTimeout(function () { button.textContent = original; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done, function () { showToast('复制失败，手动选中吧'); });
    } else {
      showToast('这个浏览器不支持一键复制，手动选中吧');
    }
  });

  document.addEventListener('click', function (event) {
    var thumb = event.target.closest('[data-thumb]');
    if (!thumb) { return; }
    var main = document.getElementById('gmain-img');
    if (main) {
      main.addEventListener('load', function () { clampRatio(main); }, { once: true });
      main.src = thumb.getAttribute('data-thumb');
    }
    document.querySelectorAll('.gthumb').forEach(function (button) {
      button.classList.toggle('on', button === thumb);
    });
    var count = document.querySelector('.gcount');
    if (count) {
      var all = Array.prototype.slice.call(document.querySelectorAll('.gthumb'));
      count.textContent = (all.indexOf(thumb) + 1) + '/' + all.length;
    }
  });

  /* ── 对称长轮询：25s 无变化服务端回 204，立刻重发；断连指数退避到 5s 封顶 ── */
  watchRatios(document);
  var backoff = 1000;
  (function poll() {
    api('/api/state?after=' + revision).then(function (response) {
      if (response.status === 204) { backoff = 1000; setLive(true); poll(); return; }
      if (!response.ok) { throw new Error(String(response.status)); }
      response.json().then(function (state) {
        backoff = 1000;
        setLive(true);
        applyState(state);
        poll();
      });
    }).catch(function () {
      setLive(false);
      setTimeout(poll, backoff);
      backoff = Math.min(backoff * 2, 5000);
    });
  })();
})();
