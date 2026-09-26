(() => {
  const toggle = document.querySelector("#app-nav-toggle");
  const navigation = document.querySelector("#app-navigation");

  if (!toggle || !navigation) return;

  const setOpen = (open, restoreFocus = false) => {
    navigation.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Navigation schließen" : "Navigation öffnen");
    if (restoreFocus) toggle.focus();
  };

  toggle.addEventListener("click", () => {
    setOpen(!navigation.classList.contains("open"));
  });

  navigation.addEventListener("click", (event) => {
    if (
      event.target instanceof Element &&
      event.target.closest("a") &&
      window.matchMedia("(max-width: 800px)").matches
    ) {
      setOpen(false);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && navigation.classList.contains("open")) {
      setOpen(false, true);
    }
  });

  window.matchMedia("(min-width: 801px)").addEventListener("change", (event) => {
    if (event.matches) setOpen(false);
  });
})();
