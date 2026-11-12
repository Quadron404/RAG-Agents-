(function() {
  const chatReturnAttr = 'data-chat-return';
  const handoffParam = 'handoff';
  const themeStorageKey = 'govlink:theme';
  const themeColorContent = { dark: '#0b0d12', light: '#fff' };
  const languageStorageKey = 'govlink:language';
  const languageOptions = ['en', 'fr', 'es'];

  if (/\bGovLinkApp\//.test(navigator.userAgent)) {
    document.documentElement.setAttribute('data-govlink-app', '');
  }

  if (
    /^\/chat\/?$/.test(location.pathname) &&
    new URLSearchParams(location.search).has(handoffParam) &&
    !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  ) {
    document.documentElement.setAttribute(chatReturnAttr, '');
    setTimeout(() => document.documentElement.removeAttribute(chatReturnAttr), 10000);
    window.addEventListener('pagereveal', (event) => event.viewTransition?.skipTransition(), { once: true });
  }

  let stored = null;
  try {
    stored = localStorage.getItem(themeStorageKey);
  } catch {
    // Blocked storage means no saved choice; System applies.
  }
  const preference = stored === 'light' || stored === 'dark' ? stored : 'system';
  const theme = preference === 'system'
    ? window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    : preference;
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', themeColorContent[theme]);

  let saved = null;
  try {
    saved = localStorage.getItem(languageStorageKey);
  } catch {
    // Blocked storage means no saved choice; English applies.
  }
  const preferred = languageOptions.includes(saved) ? saved : 'en';
  if (!/^en(?:-|$)/i.test(preferred)) {
    document.documentElement.dataset.languagePending = '';
    setTimeout(() => { delete document.documentElement.dataset.languagePending; }, 3000);
  }
})();