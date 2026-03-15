/* 甲鱼AI日报 — Custom JS */
document.addEventListener('DOMContentLoaded', function() {
  // Add target="_blank" to external links
  document.querySelectorAll('.post-content a').forEach(function(a) {
    if (a.hostname !== window.location.hostname) {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    }
  });
});
