(() => {
  "use strict";

  const verifyPanel = document.getElementById("verify-panel");
  const decisionPanel = document.getElementById("decision-panel");
  const resultPanel = document.getElementById("result-panel");
  const verifyForm = document.getElementById("verify-form");
  const decisionForm = document.getElementById("decision-form");
  const linkToken = window.location.hash.slice(1);
  let submissionToken = "";

  history.replaceState(null, "", window.location.pathname);

  function setBusy(form, busy) {
    const button = form.querySelector("button");
    button.disabled = busy;
  }

  function showResult(title, message, error = false) {
    verifyPanel.classList.add("hidden");
    decisionPanel.classList.add("hidden");
    resultPanel.classList.remove("hidden");
    resultPanel.classList.toggle("error", error);
    document.getElementById("result-title").textContent = title;
    document.getElementById("result-message").textContent = message;
  }

  async function postJson(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(
        response.status === 429
          ? "Too many attempts. Wait before trying again."
          : "This link is invalid, expired, already used, or could not be verified."
      );
    }
    return body;
  }

  if (!linkToken) {
    showResult(
      "Link unavailable",
      "Open the complete one-time link from your missed-call notification.",
      true
    );
    return;
  }

  verifyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    setBusy(verifyForm, true);
    try {
      const body = await postJson("/v1/fallback/open", {
        token: linkToken,
        confirmation_pin: document.getElementById("owner-pin").value,
      });
      document.getElementById("owner-pin").value = "";
      submissionToken = body.submission_token;
      document.getElementById("severity").textContent = body.severity;
      document.getElementById("summary").textContent = body.summary;
      document.getElementById("question").textContent = body.question;
      document.getElementById("expires").textContent =
        `This response link expires ${new Date(body.expires_at).toLocaleString()}.`;

      if (body.pending_action_summary) {
        document.getElementById("pending-wrap").classList.remove("hidden");
        document.getElementById("pending-action").textContent =
          body.pending_action_summary;
      }

      if (Array.isArray(body.owner_constraints) && body.owner_constraints.length) {
        const list = document.getElementById("constraints");
        for (const constraint of body.owner_constraints) {
          const item = document.createElement("li");
          item.textContent = constraint;
          list.appendChild(item);
        }
        document.getElementById("constraints-wrap").classList.remove("hidden");
      }

      verifyPanel.classList.add("hidden");
      decisionPanel.classList.remove("hidden");
    } catch (error) {
      showResult("Verification failed", error.message, true);
    } finally {
      setBusy(verifyForm, false);
    }
  });

  decisionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    setBusy(decisionForm, true);
    try {
      const body = await postJson("/v1/fallback/decision", {
        submission_token: submissionToken,
        outcome: document.getElementById("outcome").value,
        instruction: document.getElementById("instruction").value || null,
        confirmed: document.getElementById("confirmed").checked,
      });
      submissionToken = "";
      showResult("Response delivered", body.message_to_user);
    } catch (error) {
      showResult("Response not delivered", error.message, true);
    } finally {
      setBusy(decisionForm, false);
    }
  });
})();
