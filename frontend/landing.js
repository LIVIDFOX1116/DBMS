/*
TXGraph — landing page behavior
Adapted from TemplateMo 622 Clearwave's script: smooth-scroll, nav-on-scroll,
mobile menu, scroll-reveal and stat counters. Carousel/pricing-toggle logic
dropped — this page has neither.
*/

/* ── Smooth Scroll ── */
document.querySelectorAll('a[href^="#"]').forEach(link => {
  link.addEventListener('click', e => {
    const href = link.getAttribute('href');
    if (href === '#') {
      e.preventDefault();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
      const target = document.querySelector(href);
      if (target) { e.preventDefault(); target.scrollIntoView({ behavior: 'smooth' }); }
    }
  });
});

/* ── Nav scroll state ── */
const nav = document.getElementById('mainNav');
window.addEventListener('scroll', () => {
  nav.classList.toggle('scrolled', window.scrollY > 40);
}, { passive: true });

/* ── Mobile menu ── */
const hamburger = document.getElementById('hamburger');
const mobileMenu = document.getElementById('mobileMenu');
function openMobileMenu() {
  hamburger.classList.add('open'); mobileMenu.classList.add('open');
  hamburger.setAttribute('aria-expanded', 'true'); document.body.style.overflow = 'hidden';
}
function closeMobileMenu() {
  hamburger.classList.remove('open'); mobileMenu.classList.remove('open');
  hamburger.setAttribute('aria-expanded', 'false'); document.body.style.overflow = '';
}
hamburger.addEventListener('click', () => {
  mobileMenu.classList.contains('open') ? closeMobileMenu() : openMobileMenu();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMobileMenu(); });
mobileMenu.querySelectorAll('a').forEach(link => link.addEventListener('click', () => closeMobileMenu()));

/* ── Scroll reveal ── */
const revealObserver = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if (entry.isIntersecting) { entry.target.classList.add('visible'); revealObserver.unobserve(entry.target); }
  });
}, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
document.querySelectorAll('.reveal').forEach(el => revealObserver.observe(el));

/* ── Stat counters ── */
function animateCounter(el) {
  const target = parseFloat(el.dataset.target);
  const decimal = el.dataset.decimal;
  const duration = 1800;
  const start = performance.now();
  function step(now) {
    const progress = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 4);
    const val = eased * target;
    el.textContent = decimal ? val.toFixed(Number(decimal)) : Math.floor(val).toLocaleString();
    if (progress < 1) requestAnimationFrame(step);
    else el.textContent = decimal ? target.toFixed(Number(decimal)) : target.toLocaleString();
  }
  requestAnimationFrame(step);
}
const statObserver = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.querySelectorAll('.stat-num').forEach(animateCounter);
      statObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.4 });
document.querySelectorAll('.stats-grid').forEach(el => statObserver.observe(el));

/* ── FAQ accordion ── */
const faqItems = document.querySelectorAll('.faq-item');
let allOpen = false;
faqItems.forEach(item => {
  const question = item.querySelector('.faq-question');
  question.addEventListener('click', () => toggleFaq(item));
  question.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleFaq(item); }
  });
});
function toggleFaq(item) {
  const isOpen = item.classList.contains('open');
  item.classList.toggle('open', !isOpen);
  item.querySelector('.faq-question').setAttribute('aria-expanded', !isOpen);
}
const faqToggleAllBtn = document.getElementById('faqToggleAll');
const faqToggleIcon = document.getElementById('faqToggleIcon');
faqToggleAllBtn.addEventListener('click', () => {
  allOpen = !allOpen;
  faqItems.forEach(item => {
    item.classList.toggle('open', allOpen);
    item.querySelector('.faq-question').setAttribute('aria-expanded', String(allOpen));
  });
  faqToggleIcon.textContent = allOpen ? '−' : '+';
  faqToggleAllBtn.lastChild.textContent = allOpen ? ' Collapse all' : ' Expand all';
});
