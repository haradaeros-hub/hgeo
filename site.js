const filters = document.querySelectorAll('[data-filter]');
filters.forEach(button => button.addEventListener('click', () => {
  filters.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  document.querySelectorAll('.gallery figure').forEach(card => {
    card.hidden = button.dataset.filter !== 'all' && card.dataset.category !== button.dataset.filter;
  });
}));
const lightbox = document.querySelector('#lightbox');
const picture = document.querySelector('#large-image');
let opener;
document.querySelectorAll('.image-open').forEach(button => button.addEventListener('click', () => {
  opener = button;
  picture.src = button.dataset.image;
  picture.alt = button.dataset.caption;
  document.querySelector('#image-caption').textContent = button.dataset.caption;
  lightbox.showModal();
  document.body.classList.add('modal-open');
}));
document.querySelector('#close-lightbox').addEventListener('click', () => lightbox.close());
lightbox.addEventListener('click', event => { if (event.target === lightbox) lightbox.close(); });
lightbox.addEventListener('close', () => {
  document.body.classList.remove('modal-open');
  if (opener) opener.focus();
});
