// note-brain 页面脚本（原生 JS，无框架；具体逻辑在各模板的 script 块内）

// CSRF：登录启用时，所有非安全方法的 fetch 自动带 X-CSRF-Token 头（§9）。
// token 由 base.html 的 <meta name="csrf-token"> 注入（未登录/未启用时为空串）。
(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta ? meta.getAttribute('content') : '';
  window.CSRF_TOKEN = token || '';
  var origFetch = window.fetch;
  window.fetch = function (url, opts) {
    opts = opts || {};
    var method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS' && token) {
      if (!opts.headers) opts.headers = {};
      if (opts.headers instanceof Headers) {
        if (!opts.headers.has('X-CSRF-Token')) opts.headers.set('X-CSRF-Token', token);
      } else if (!opts.headers['X-CSRF-Token']) {
        opts.headers['X-CSRF-Token'] = token;
      }
    }
    return origFetch(url, opts);
  };
})();

// 退出登录（导航栏链接，登录启用时存在）
document.addEventListener('click', function (e) {
  var link = e.target.closest && e.target.closest('#logout-link');
  if (!link) return;
  e.preventDefault();
  fetch('/api/logout', {method: 'POST'}).then(function () {
    location.href = '/login';
  }).catch(function () {
    location.href = '/login';
  });
});
