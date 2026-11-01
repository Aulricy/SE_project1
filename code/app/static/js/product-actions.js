"use strict";

const confirmation = document.querySelector("#product-confirmation");
let pendingForm = null;
let confirmedForm = null;

if (confirmation && typeof confirmation.showModal === "function") {
  document.querySelectorAll(".product-operation").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (confirmedForm === form) {
        form.querySelector('button[type="submit"]').disabled = true;
        return;
      }
      event.preventDefault();
      pendingForm = form;
      document.querySelector("#confirmation-message").textContent = form.dataset.confirm;
      confirmation.showModal();
    });
  });
  document.querySelector("#cancel-operation").addEventListener("click", () => {
    confirmation.close();
    pendingForm = null;
  });
  document.querySelector("#confirm-operation").addEventListener("click", () => {
    confirmedForm = pendingForm;
    confirmation.close();
    if (confirmedForm) confirmedForm.requestSubmit();
  });
}
