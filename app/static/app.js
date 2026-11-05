// Small enhancements on top of plain HTML forms: sample picker, busy buttons,
// confirmation dialogs, and polling while n8n works on a lead. Every page still
// works without this file.

// n8n scales to zero and takes a minute or two to wake, so start it while the visitor
// picks a sample.
if (document.querySelector(".sample") && navigator.sendBeacon) navigator.sendBeacon("/n8n/wake");

// Picking a sample fills the composer with its message, sender and channel.
document.querySelectorAll(".sample input").forEach((radio) => {
  radio.addEventListener("change", () => {
    const row = radio.closest(".sample").dataset;
    const form = radio.form;
    form.querySelector("#message").value = row.message;
    form.querySelector('[data-bind="name"]').textContent = row.name;
    form.querySelector('[data-bind="company"]').textContent = row.company || "No company given";
    const channel = form.querySelector(`input[name="channel"][value="${row.channel}"]`);
    if (channel) channel.checked = true;
  });
});

// A submitted form shows that it is working and cannot be sent twice.
document.querySelectorAll("form[data-busy]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (form.dataset.sent) return event.preventDefault();
    form.dataset.sent = "1";
    const button = event.submitter;
    if (button && button.dataset.busyLabel) {
      // A disabled submitter drops its name/value, so keep it in a hidden field.
      if (button.name) {
        const hidden = Object.assign(document.createElement("input"), { type: "hidden", name: button.name, value: button.value });
        form.append(hidden);
      }
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
      const text = button.querySelector(".label-text");
      if (text) text.textContent = button.dataset.busyLabel;
    }
    form.querySelectorAll(".sending").forEach((el) => (el.hidden = false));
  });
});

// Buttons or forms marked data-confirm ask first, in a <dialog>.
function confirmFirst(dialogId, proceed) {
  const dialog = document.getElementById(dialogId);
  if (!dialog || !dialog.showModal) return proceed();
  dialog.showModal();
  dialog.querySelectorAll("button").forEach((b) => {
    b.onclick = () => {
      dialog.close();
      if (b.value === "confirm") proceed();
    };
  });
}
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (form.dataset.confirmed) return;
    event.preventDefault();
    confirmFirst(form.dataset.confirm, () => {
      form.dataset.confirmed = "1";
      form.requestSubmit();
    });
  });
});
document.querySelectorAll("button[data-confirm]").forEach((button) => {
  button.addEventListener("click", (event) => {
    if (button.dataset.confirmed) return;
    event.preventDefault();
    confirmFirst(button.dataset.confirm, () => {
      button.dataset.confirmed = "1";
      button.form.requestSubmit(button);
    });
  });
});

// While n8n works on a lead, check its state every two seconds and reload when it
// changes, instead of refreshing the whole page on a timer.
const poll = document.querySelector("[data-poll]");
if (poll) {
  const started = Date.now();
  const tick = async () => {
    try {
      const r = await fetch(poll.dataset.poll, { headers: { Accept: "application/json" } });
      if (r.ok) {
        const s = await r.json();
        if (s.status !== poll.dataset.status || s.updated_at !== poll.dataset.updated) return location.reload();
      }
    } catch (e) {
      // A network blip; try again on the next tick.
    }
    if (Date.now() - started < 180000) setTimeout(tick, 2000);
  };
  setTimeout(tick, 2000);
}
