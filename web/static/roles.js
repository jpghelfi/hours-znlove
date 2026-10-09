// Saves a PM / Account manager <select class="role-pick"> through
// POST /api/project/role. Shared by /projects/roles, /assignments and /project.
// dataset.prev holds the last known-saved value, so a failed save reverts to
// it rather than leaving a value on screen that was never written.
(function () {
  function remember(sel) { sel.dataset.prev = sel.value; }
  document.querySelectorAll('.role-pick').forEach(remember);

  // a page shows failures in its own [data-role-notice] banner when it has
  // one, else in the app's dialog
  var noticeTimer = null;
  function notice(msg) {
    var el = document.querySelector('[data-role-notice]');
    if (!el) { if (window.zAlert) zAlert(msg); return; }
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(function () { el.hidden = true; }, 4000);
  }

  window.saveRole = async function (sel) {
    var prev = sel.dataset.prev;
    var body = {project_id: sel.dataset.project, role: sel.dataset.role,
                person_id: sel.value || null};
    sel.classList.remove('bad');
    sel.disabled = true;
    try {
      var res = await fetch('/api/project/role', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      var data = await res.json();
      if (!data.ok) throw new Error(data.error || 'could not save');
      remember(sel);
      sel.classList.remove('is-off');
      sel.dispatchEvent(new CustomEvent('role-saved', {bubbles: true}));
    } catch (e) {
      sel.value = prev;
      sel.classList.add('bad');
      var why = e && e.message && e.message !== 'Failed to fetch' ? e.message : 'check your connection.';
      notice("Couldn't save — " + why);
    } finally {
      sel.disabled = false;
    }
  };
})();
