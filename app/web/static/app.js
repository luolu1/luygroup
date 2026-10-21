/* GroupGuard panel interactions. */
(() => {
  "use strict";

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const submitter = event.submitter;
    const message =
      (submitter && submitter.dataset ? submitter.dataset.confirm : "") ||
      form.dataset.confirm;
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", (event) => {
    const opener = event.target.closest("[data-modal-open]");
    if (opener) {
      const dialog = document.getElementById(opener.getAttribute("data-modal-open") || "");
      if (dialog && typeof dialog.showModal === "function") {
        dialog.showModal();
      } else if (dialog) {
        dialog.setAttribute("open", "");
      }
      return;
    }

    const modalCloser = event.target.closest("[data-modal-close]");
    if (modalCloser) {
      const dialog = modalCloser.closest("dialog");
      if (dialog) dialog.close();
      return;
    }

    if (event.target instanceof HTMLDialogElement && event.target.open) {
      const rect = event.target.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      ) {
        event.target.close();
      }
      return;
    }

    const button = event.target.closest("[data-copy]");
    if (button) {
      const text = button.getAttribute("data-copy") || "";
      navigator.clipboard.writeText(text).then(
        () => {
          const old = button.textContent;
          button.textContent = "已复制";
          setTimeout(() => { button.textContent = old; }, 1200);
        },
        () => window.alert("复制失败，请手动选择文本复制。")
      );
      return;
    }

    const closer = event.target.closest("[data-dismiss]");
    if (closer) {
      const banner = closer.closest(".banner");
      if (banner) banner.remove();
    }
  });

  // Keep the current group tab selected after a refresh when a hash is present.
  const active = document.querySelector(".subnav a.active");
  if (active && active.dataset.keep !== "false") {
    active.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
})();
