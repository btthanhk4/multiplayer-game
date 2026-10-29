// Applies the saved colour theme and board style before first paint (a classic blocking script, so there is no
// flash) and puts a toggle and a board picker in the header. Dark and wood are the defaults, like the main OTTv2 page.
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
  // Board and piece style: wood (default), the original flat look, or marble. Only changes colours, never the game.
  var SKIN_KEY = 'ottv2-skin', SKINS = [['wood', 'Gỗ'], ['classic', 'Nguyên bản'], ['marble', 'Cẩm thạch']];
  function readSkin() { try { return localStorage.getItem(SKIN_KEY); } catch (e) { return null; } }
  var skin = readSkin();
  if (!SKINS.some(function (s) { return s[0] === skin; })) skin = 'wood';
  root.setAttribute('data-skin', skin);
  function picker() {
    var label = document.createElement('label'), text = document.createElement('span'), select = document.createElement('select');
    label.className = 'skin-picker';
    text.className = 'label';
    text.textContent = 'Bàn & quân';
    select.setAttribute('aria-label', 'Kiểu bàn và quân cờ');
    for (var i = 0; i < SKINS.length; i++) {
      var option = document.createElement('option');
      option.value = SKINS[i][0];
      option.textContent = SKINS[i][1];
      select.appendChild(option);
    }
    select.value = skin;
    label.appendChild(text);
    label.appendChild(select);
    return label;
  }
  document.addEventListener('DOMContentLoaded', function () {
    var hosts = document.querySelectorAll('.masthead nav, .gate');
    for (var i = 0; i < hosts.length; i++) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'theme-toggle';
      button.setAttribute('aria-label', 'Đổi nền sáng hoặc tối');
      hosts[i].insertBefore(button, hosts[i].firstChild);
    }
    var navs = document.querySelectorAll('.masthead nav, [data-skin-slot]');
    for (var j = 0; j < navs.length; j++) navs[j].insertBefore(picker(), navs[j].firstChild);
    document.addEventListener('change', function (event) {
      var select = event.target;
      if (!select.closest || !select.closest('.skin-picker')) return;
      skin = select.value;
      root.setAttribute('data-skin', skin);
      try { localStorage.setItem(SKIN_KEY, skin); } catch (e) { /* private mode: keep it for this page only */ }
      var all = document.querySelectorAll('.skin-picker select');
      for (var k = 0; k < all.length; k++) all[k].value = skin;
    });
    apply(theme);
    document.addEventListener('click', function (event) {
      if (!event.target.closest || !event.target.closest('.theme-toggle')) return;
      theme = theme === 'dark' ? 'light' : 'dark';
      write(theme);
      apply(theme);
    });
  });
})();
