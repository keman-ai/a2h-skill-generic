/* desk UI 页面脚本。刻意保持薄：长轮询换片段、转发点击、忙碌提示条、
   图片比例钳制 —— 没有第二套模板（HTML 全部由服务端渲染，见 deskui_pages.py 头注）。
   通过 CSP 外置加载；启动状态从 #boot JSON 数据岛读（CSP 下不执行、无内联脚本）。

   长轮询等在**全局** revision 上（busy 的灰/亮要即刻反映），但只有 view_rev
   变了才换 HTML —— 否则每次灰/亮都会冲掉图集选中态和滚动位置。 */
(function () {
  'use strict';

  var boot = JSON.parse(document.getElementById('boot').textContent || '{}');
  var revision = boot.revision || 0;
  var viewRev = boot.view_rev || 0;
  var busyState = null;
  var busyShownAt = null;
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

  /* ── AI 忙碌提示条：busy 来自服务端状态；只灰 AI 按钮不锁页面；
     90 秒后追加手动解除按钮（agent 可能死了） ── */
  function renderBusy(busy) {
    busyState = busy || null;
    document.body.classList.toggle('busy', !!busy);
    var bar = document.getElementById('busybar');
    if (!bar) { return; }
    if (!busy) { bar.hidden = true; busyShownAt = null; return; }
    bar.hidden = false;
    document.getElementById('busy-hint').textContent = busy.hint || 'AI 正在进行你的上一个指令…';
    if (busyShownAt === null) { busyShownAt = Date.now() - (busy.sinceSeconds || 0) * 1000; }
  }

  setInterval(function () {
    if (busyShownAt === null) { return; }
    var seconds = Math.floor((Date.now() - busyShownAt) / 1000);
    var since = document.getElementById('busy-since');
    if (since) { since.textContent = '已等待 ' + seconds + ' 秒'; }
    var unlock = document.getElementById('busy-unlock');
    if (unlock) { unlock.hidden = seconds < 90; }
  }, 1000);

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
    revision = state.revision;
    renderBusy(state.busy || null);
    /* 内容没变就不动 DOM —— 别冲掉图集选中态和滚动位置 */
    if (state.view_rev === viewRev) { return; }
    viewRev = state.view_rev;
    var view = document.getElementById('view');
    view.innerHTML = state.html;
    watchRatios(view);
    window.scrollTo(0, 0);
  }

  function showError(message) {
    showToast(message);
  }

  function submit(action, onDone) {
    api('/api/human-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, expected_revision: revision })
    }).then(function (response) {
      if (response.ok) { if (onDone) { onDone(true); } return; }
      response.json().then(function (payload) {
        /* 409 = 状态刚被推进，长轮询马上带来新状态，不当错误惊动人；
           423 = AI 还在进行上一个指令 —— 轻提示即可 */
        if (response.status === 423) { showToast(payload.error || 'AI 正在进行你的上一个指令'); }
        else if (response.status !== 409) { showError(payload.error || ('操作失败 ' + response.status)); }
        if (onDone) { onDone(false); }
      }).catch(function () { if (onDone) { onDone(false); } });
    }).catch(function () { setLive(false); if (onDone) { onDone(false); } });
  }

  /* ── 点击转发：一切动作都从服务端渲染的 data-act 里取，页面自己造不出动作。
     busy 期间 AI 按钮（data-agent）本地拦下出 toast —— 服务端 423 仍是权威闸 ── */
  document.addEventListener('click', function (event) {
    var target = event.target.closest('[data-act]');
    if (!target) { return; }
    if (busyState && target.hasAttribute('data-agent')) {
      showToast('AI 正在进行你的上一个指令 —— 等它完成再点，浏览不受影响');
      return;
    }
    submit(JSON.parse(target.getAttribute('data-act')));
  });

  document.addEventListener('click', function (event) {
    var thumb = event.target.closest('[data-thumb]');
    if (!thumb) { return; }
    var main = document.getElementById('gmain-img');
    if (main) { main.src = thumb.getAttribute('data-thumb'); }
    document.querySelectorAll('.gthumb').forEach(function (button) {
      button.classList.toggle('on', button === thumb);
    });
    var count = document.querySelector('.gcount');
    if (count) {
      var all = Array.prototype.slice.call(document.querySelectorAll('.gthumb'));
      count.textContent = (all.indexOf(thumb) + 1) + '/' + all.length;
    }
  });

  document.addEventListener('click', function (event) {
    if (event.target.id !== 'busy-unlock') { return; }
    submit({ type: 'unlock' });
  });

  /* ── 对称长轮询：25s 无变化服务端回 204，立刻重发；断连指数退避到 5s 封顶 ── */
  watchRatios(document);
  renderBusy(boot.busy || null);
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
