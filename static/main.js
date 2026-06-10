/**
 * GOBA INVEST — Shared JavaScript Utilities
 * Tema, dil, saat gibi ortak fonksiyonlar burada.
 */

// ── Tema Değiştirme ──────────────────────────────────────────────────────────
function initTheme() {
    const toggle = document.getElementById('theme-toggle');
    if (!toggle) return;
    const html = document.documentElement;
    toggle.addEventListener('click', () => {
        const current = html.getAttribute('data-theme') || 'dark';
        const next = current === 'dark' ? 'light' : 'dark';
        html.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
    });
}

// ── Canlı Saat ───────────────────────────────────────────────────────────────
function initClock() {
    function tick() {
        const el = document.querySelector('#live-clock span');
        if (el) {
            el.textContent = new Date().toLocaleTimeString('tr-TR', { hour12: false });
        }
    }
    tick();
    setInterval(tick, 1000);
}

// ── Dil Değiştirme (ortak altyapı) ──────────────────────────────────────────
function applyTranslations(lang, translations) {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (translations[lang] && translations[lang][key]) {
            el.innerHTML = translations[lang][key];
        }
    });
    // Tarih metinleri (data-tr / data-en)
    document.querySelectorAll('.date-text').forEach(el => {
        const trText = el.getAttribute('data-tr');
        const enText = el.getAttribute('data-en');
        if (trText && enText) el.innerText = lang === 'en' ? enText : trText;
    });
    document.documentElement.setAttribute('lang', lang);
    localStorage.setItem('lang', lang);
}

function initLangToggle(getTranslationsFn, onLangChange) {
    const toggle = document.getElementById('lang-toggle');
    const textEl = document.getElementById('lang-text');
    if (!toggle) return;

    toggle.addEventListener('click', () => {
        const current = document.documentElement.getAttribute('lang') || 'tr';
        const next = current === 'tr' ? 'en' : 'tr';
        if (textEl) textEl.innerText = next.toUpperCase();
        applyTranslations(next, getTranslationsFn());
        if (onLangChange) onLangChange(next);
    });

    // Başlangıç dili
    const saved = localStorage.getItem('lang') || 'tr';
    if (textEl) textEl.innerText = saved.toUpperCase();
    applyTranslations(saved, getTranslationsFn());
    if (onLangChange) onLangChange(saved);
}

// ── Başlat ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    initClock();
    // Son güncelleme saatini hemen göster
    const lastUpdateEl = document.getElementById('last-updated-time');
    if (lastUpdateEl && lastUpdateEl.innerText === '--:--:--') {
        lastUpdateEl.innerText = new Date().toLocaleTimeString('tr-TR', { hour12: false });
    }
});
