"use strict";

const form = document.querySelector("#admin-account-form");
const dialog = document.querySelector("#approval-dialog");

if (form && dialog && typeof dialog.showModal === "function") {
  let approved = false;
  form.addEventListener("submit", (event) => {
    if (approved) {
      form.querySelector('button[type="submit"]').disabled = true;
      return;
    }
    event.preventDefault();
    document.querySelector("#approval-username").textContent = form.elements.username.value.trim();
    dialog.showModal();
  });
  document.querySelector("#cancel-approval").addEventListener("click", () => dialog.close());
  document.querySelector("#confirm-approval").addEventListener("click", () => {
    approved = true;
    dialog.close();
    form.requestSubmit();
  });
}
