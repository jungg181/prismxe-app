// Shared UI helpers
function openModal(html) {
  const root = document.getElementById('modal-root');
  root.innerHTML = `<div class="overlay" onclick="if(event.target===this)closeModal()"><div class="modal">${html}<div style="margin-top:12px;text-align:right"><button class="btn" onclick="closeModal()">Close</button></div></div></div>`;
}
function closeModal() {
  document.getElementById('modal-root').innerHTML = '';
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
