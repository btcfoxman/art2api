(() => {
  // Public generation sitekey and callbacks observed in Artlist's loaded client.
  // No model selection, Generate button, fetch mutation or challenge solving.
  const container = document.createElement('div');
  container.id = 'art2api-normal-verification';
  document.body.append(container);
  const update = value => { window.__art2apiVerification = value; };
  update({status: 'waiting'});
  window.__art2apiVerificationWidget = window.turnstile.render(container, {
    sitekey: '0x4AAAAAAD-4st6Pct76Aua3',
    appearance: 'interaction-only',
    theme: 'dark',
    callback: token => update({status: 'ready', token}),
    'error-callback': code => update({status: 'error', code: typeof code === 'string' && /^[a-zA-Z0-9_-]{1,64}$/.test(code) ? code : 'widget-error'}),
    'unsupported-callback': () => update({status: 'unsupported', code: 'unsupported-browser'}),
    'timeout-callback': () => update({status: 'timeout', code: 'challenge-timeout'}),
    'before-interactive-callback': () => update({status: 'interaction_required'}),
    'expired-callback': () => update({status: 'waiting'})
  });
  return true;
})()
