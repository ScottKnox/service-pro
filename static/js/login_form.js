document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("login-form");
  const usernameInput = document.getElementById("username");

  if (!form || !usernameInput) {
    return;
  }

  form.addEventListener("submit", () => {
    usernameInput.value = usernameInput.value.trimEnd();
  });
});