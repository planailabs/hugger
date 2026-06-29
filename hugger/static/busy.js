// Busy feedback for plain (non-Datastar) form submits: add the `is-busy` class
// to the submit button so the THEME rules (recolor + busy label) show during the
// brief moment before the full-page navigation. Datastar buttons handle their
// own busy state via an indicator signal, so they don't need this.
document.addEventListener('submit', function (e) {
  var b = e.submitter;
  if (b && b.tagName === 'BUTTON') b.classList.add('is-busy');
}, true);
