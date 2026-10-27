// Applies the saved colour theme before first paint (a classic blocking script, so there is no flash) and
// puts a toggle in the header. Dark is the default, like the main OTTv2 page.
(function () {
  var KEY = 'ottv2-theme', root = document.documentElement;
  function read() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function write(value) { try { localStorage.setItem(KEY, value); } catch (e) { /* private mode: keep it for this page only */ } }
  function apply(theme) {
    root.setAttribute('data-theme', theme);
    var meta = document.querySelector('meta[name=theme-color]');
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#181c18' : '#eeeae0');
    var buttons = document.querySelectorAll('.theme-toggle');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-pressed', String(theme === 'dark'));
      buttons[i].innerHTML = theme === 'dark'
        ? '<span aria-hidden="true">☾</span><span class="label">Nền tối</span>'
        : '<span aria-hidden="true">☀</span><span class="label">Nền sáng</span>';
    }
  }
  var theme = read() === 'light' ? 'light' : 'dark';
  apply(theme);
  document.addEventListener('DOMContentLoaded', function () {
    var hosts = document.querySelectorAll('.masthead nav, .gate');
    for (var i = 0; i < hosts.length; i++) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'theme-toggle';
      button.setAttribute('aria-label', 'Đổi nền sáng hoặc tối');
      hosts[i].insertBefore(button, hosts[i].firstChild);
    }
    apply(theme);
    document.addEventListener('click', function (event) {
      if (!event.target.closest || !event.target.closest('.theme-toggle')) return;
      theme = theme === 'dark' ? 'light' : 'dark';
      write(theme);
      apply(theme);
    });
  });
})();
