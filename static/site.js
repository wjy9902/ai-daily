document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".post-content a").forEach((link) => {
    if (link.hostname !== window.location.hostname) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
  });

  const button = document.createElement("button");
  button.id = "btt";
  button.type = "button";
  button.textContent = "↑";
  button.setAttribute("aria-label", "返回顶部");
  button.hidden = true;
  document.body.appendChild(button);

  const updateVisibility = () => {
    const visible = window.scrollY > 500;
    button.hidden = !visible;
    button.tabIndex = visible ? 0 : -1;
  };
  window.addEventListener("scroll", updateVisibility, { passive: true });
  updateVisibility();
  button.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
});
