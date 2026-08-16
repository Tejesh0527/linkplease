document.addEventListener("DOMContentLoaded", () => {
  const statSent = document.getElementById("statSent");
  const statQueued = document.getElementById("statQueued");
  const statFailed = document.getElementById("statFailed");
  const statBlocked = document.getElementById("statBlocked");

  const ruleForm = document.getElementById("ruleForm");
  const ruleKeyword = document.getElementById("ruleKeyword");
  const ruleMessage = document.getElementById("ruleMessage");
  const rulesList = document.getElementById("rulesList");
  const ruleCount = document.getElementById("ruleCount");

  const simForm = document.getElementById("simForm");
  const simWebhook = document.getElementById("simWebhook");
  const simCount = document.getElementById("simCount");
  const simDuration = document.getElementById("simDuration");
  const consoleOutput = document.getElementById("consoleOutput");

  const submitForm = document.getElementById("submitForm");
  const subEmail = document.getElementById("subEmail");
  const subRepo = document.getElementById("subRepo");
  const subWorkingUrl = document.getElementById("subWorkingUrl");
  const subLoom = document.getElementById("subLoom");
  const subParts = document.getElementById("subParts");
  const subDate = document.getElementById("subDate");

  const apiKeyInput = document.getElementById("apiKeyInput");
  const saveKeyBtn = document.getElementById("saveKeyBtn");
  const keyStatusText = document.getElementById("keyStatusText");
  const statusDot = document.getElementById("statusDot");

  // Auto-fill Webhook URL default
  simWebhook.value = `${window.location.origin}/webhook`;
  subWorkingUrl.value = window.location.origin;

  // Fetch API Key Config
  async function fetchConfig() {
    try {
      const res = await fetch("/api/config");
      const data = await res.json();
      if (data.api_key) {
        apiKeyInput.value = data.api_key;
        keyStatusText.textContent = "API Key Connected";
        statusDot.classList.remove("offline");
      } else {
        keyStatusText.textContent = "No API Key Set";
        statusDot.classList.add("offline");
      }
    } catch (err) {
      console.error("Config fetch error:", err);
    }
  }

  // Save API Key Config
  saveKeyBtn.addEventListener("click", async () => {
    const newKey = apiKeyInput.value.trim();
    if (!newKey) return;
    try {
      await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: newKey })
      });
      alert("API Key updated successfully!");
      fetchConfig();
    } catch (err) {
      alert("Error saving API Key: " + err);
    }
  });

  // Fetch Live Stats
  async function fetchStats() {
    try {
      const res = await fetch("/stats");
      if (!res.ok) return;
      const data = await res.json();
      statSent.textContent = data.sent || 0;
      statQueued.textContent = data.queued || 0;
      statFailed.textContent = data.failed || 0;
      statBlocked.textContent = data.duplicates_blocked || 0;
    } catch (err) {
      console.error("Stats fetch error:", err);
    }
  }

  // Fetch Rules
  async function fetchRules() {
    try {
      const res = await fetch("/rules");
      if (!res.ok) return;
      const rules = await res.json();
      ruleCount.textContent = rules.length;

      if (rules.length === 0) {
        rulesList.innerHTML = '<p style="color: var(--text-muted); font-size: 0.9rem;">No rules configured yet.</p>';
        return;
      }

      rulesList.innerHTML = rules.map(r => `
        <div class="rule-card">
          <div class="rule-header">
            <span class="rule-badge">Keyword: ${escapeHtml(r.keyword)}</span>
            <span style="font-size: 0.75rem; color: var(--text-muted);">${r.rule_id}</span>
          </div>
          <div class="rule-msg">${escapeHtml(r.dm_message)}</div>
        </div>
      `).join("");

    } catch (err) {
      console.error("Rules fetch error:", err);
    }
  }

  // Create Rule
  ruleForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const keyword = ruleKeyword.value.trim();
    const dm_message = ruleMessage.value.trim();

    if (!keyword || !dm_message) return;

    try {
      const res = await fetch("/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keyword, dm_message })
      });

      if (res.status === 201) {
        ruleKeyword.value = "";
        ruleMessage.value = "";
        fetchRules();
      } else {
        alert("Failed to create rule");
      }
    } catch (err) {
      alert("Error creating rule: " + err);
    }
  });

  // Simulation Runner
  simForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const webhookUrl = simWebhook.value.trim();
    const count = parseInt(simCount.value, 10);
    const duration = parseInt(simDuration.value, 10);

    const apiKey = apiKeyInput.value.trim();
    if (!apiKey) {
      alert("Please set your API Key first!");
      return;
    }

    consoleOutput.style.display = "block";
    consoleOutput.textContent = `[INIT] Starting simulation of ${count} events over ${duration}s...\nTarget: ${webhookUrl}\n`;

    try {
      const res = await fetch("https://pseudogram-api.onrender.com/v1/simulate/start", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": apiKey
        },
        body: JSON.stringify({
          webhook_url: webhookUrl,
          count: count,
          duration_seconds: duration
        })
      });

      if (!res.ok) {
        const errText = await res.text();
        consoleOutput.textContent += `[ERROR] Simulation trigger failed (${res.status}): ${errText}\n`;
        return;
      }

      const data = await res.json();
      const runId = data.run_id;
      consoleOutput.textContent += `[SUCCESS] Simulation started! Run ID: ${runId}\n[POLLING] Waiting for completion...\n`;

      // Poll truth output
      pollTruth(runId, apiKey);

    } catch (err) {
      consoleOutput.textContent += `[ERROR] Network request error: ${err}\n`;
    }
  });

  async function pollTruth(runId, apiKey) {
    let attempts = 0;
    const interval = setInterval(async () => {
      attempts++;
      try {
        const res = await fetch(`https://pseudogram-api.onrender.com/v1/simulate/${runId}/truth`, {
          headers: { "X-API-Key": apiKey }
        });
        if (res.ok) {
          const truth = await res.json();
          if (truth.status === "complete" || attempts >= 20) {
            clearInterval(interval);
            consoleOutput.textContent += `\n=== SIMULATION TRUTH REPORT ===\n${JSON.stringify(truth, null, 2)}\n`;
          } else {
            consoleOutput.textContent += `.`;
          }
        }
      } catch (err) {
        console.error("Truth poll error:", err);
      }
    }, 2000);
  }

  // Submission Form Handler
  submitForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    const payload = {
      email: subEmail.value.trim(),
      github_repo: subRepo.value.trim(),
      working_url: subWorkingUrl.value.trim(),
      loom_url: subLoom.value.trim(),
      parts_completed: subParts.value,
      start_date: subDate.value
    };

    if (!confirm(`Ready to submit assignment to pseudogram-api?\n\n${JSON.stringify(payload, null, 2)}`)) {
      return;
    }

    try {
      const res = await fetch("https://pseudogram-api.onrender.com/v1/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      const resText = await res.text();
      if (res.ok) {
        alert("🎉 SUBMISSION ACCEPTED!\n\nResponse: " + resText);
      } else {
        alert("⚠️ Submission error (" + res.status + "): " + resText);
      }
    } catch (err) {
      alert("Error submitting: " + err);
    }
  });

  function escapeHtml(str) {
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // Initial loads and auto-refresh
  fetchConfig();
  fetchStats();
  fetchRules();

  setInterval(fetchStats, 2000);
});
