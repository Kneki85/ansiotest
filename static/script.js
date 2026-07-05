// Sincronizar sliders con su valor visible
document.querySelectorAll("input[type='range']").forEach((input) => {
  const valEl = document.getElementById("val-" + input.id);
  if (valEl) input.addEventListener("input", () => { valEl.textContent = input.value; });
});

// Recolectar valores del formulario
function getFormData() {
  const fields = [
    "autoeficacia", "apoyo_social", "horas_estudio",
    "promedio", "presion_academica", "ausentismo",
    "horas_sueno", "actividad_fisica", "consumo_cafeina",
  ];
  return Object.fromEntries(fields.map(f => [f, parseFloat(document.getElementById(f).value)]));
}

async function submitForm() {
  const btn = document.getElementById("submit-btn");
  const errEl = document.getElementById("error-msg");

  btn.disabled = true;
  btn.textContent = "Analizando...";
  errEl.style.display = "none";

  try {
    const res = await fetch("/predict", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(getFormData()),
    });

    const data = await res.json();
    if (data.error) throw new Error(data.error);

    renderResult(data);
    showScreen("result");
  } catch (err) {
    errEl.textContent = "Error al procesar: " + err.message;
    errEl.style.display = "block";
  } finally {
    btn.disabled = false;
    btn.textContent = "Evaluar nivel de ansiedad";
  }
}

function renderResult(data) {
  // Badge de riesgo
  const badge = document.getElementById("risk-badge");
  badge.className = "risk-badge " + data.color;
  badge.innerHTML = `<span>${data.icon}</span><span>Riesgo ${data.level}</span>`;

  document.getElementById("result-desc").textContent = data.description;

  // Barras de probabilidad
  const colorMap = { Bajo: "green", Moderado: "yellow", Alto: "red" };
  document.getElementById("prob-bars").innerHTML = Object.entries(data.probabilities)
    .map(([label, pct]) => `
      <div class="prob-row">
        <span class="label">${label}</span>
        <div class="bar-track">
          <div class="bar-fill ${colorMap[label]}" style="width: 0%" data-target="${pct}"></div>
        </div>
        <span class="pct">${pct}%</span>
      </div>`)
    .join("");

  // Animar barras tras el render del DOM
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.querySelectorAll(".bar-fill[data-target]").forEach(el => {
      el.style.width = el.dataset.target + "%";
    });
  }));

  // Recomendaciones
  document.getElementById("tips-list").innerHTML =
    data.tips.map(t => `<li>${t}</li>`).join("");
}

function showScreen(name) {
  document.getElementById("form-screen").style.display = name === "form" ? "block" : "none";
  document.getElementById("result-screen").style.display = name === "result" ? "block" : "none";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function goBack() {
  showScreen("form");
}
